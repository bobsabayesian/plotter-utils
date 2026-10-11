import re
import time
from collections import deque

import serial
from serial.tools import list_ports

__all__ = [
    "GrblConnection", "GrblError", "find_ports", "read_gcode", "check_gcode",
    "clean_gcode_line", "DEFAULT_BAUD",
]

GRBL_RX_BUFFER = 127  # bytes in GRBL's serial RX buffer
DEFAULT_BAUD = 115200

# GRBL real-time commands: single bytes, not newline-terminated, bypass the buffer
FEED_HOLD = b"!"
CYCLE_START = b"~"
SOFT_RESET = b"\x18"
STATUS_QUERY = b"?"


class GrblError(Exception):
    pass


def find_ports():
    """
    Lists serial ports that look like USB devices, as (device, description) pairs.
    """
    return [(p.device, p.description) for p in list_ports.comports() if "USB" in (p.hwid or "") or "ACM" in p.device]


def clean_gcode_line(line):
    """
    Strips ';' and '(...)' comments and whitespace. Returns '' for blank lines.
    """
    line = re.sub(r"\(.*?\)", "", line)
    line = line.split(";", 1)[0]
    return line.strip()


def read_gcode(filename):
    """
    Reads a gcode file and returns the list of non-empty, comment-free lines.
    """
    with open(filename) as f:
        lines = (clean_gcode_line(l) for l in f)
        return [l for l in lines if l]


def check_gcode(filename):
    """
    Summarizes a gcode file without sending anything: line count, whether it
    uses the laser (M3/M4), and the XY extents of G0/G1 moves.
    """
    lines = read_gcode(filename)
    laser = any(re.search(r"\bM0?[34]\b", l, re.I) for l in lines)
    xs, ys = [], []
    for l in lines:
        for axis, acc in (("X", xs), ("Y", ys)):
            m = re.search(rf"{axis}(-?\d+\.?\d*)", l, re.I)
            if m:
                acc.append(float(m.group(1)))
    info = {
        "lines": len(lines),
        "uses_laser_or_spindle": laser,
        "x_range": (min(xs), max(xs)) if xs else None,
        "y_range": (min(ys), max(ys)) if ys else None,
    }
    print(info)
    return info


class GrblConnection:
    """
    A connection to a GRBL controller over USB serial. Usable as a context manager:

        with GrblConnection("/dev/ttyUSB0") as g:
            g.send_file("drawing.gcode")
    """

    def __init__(self, port, baud=DEFAULT_BAUD, timeout=1.0):
        self.ser = serial.Serial(port, baud, timeout=timeout)
        self._wake()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.ser.close()

    def _wake(self):
        # Opening the port resets most Arduino-based boards; wait for the banner
        time.sleep(2)
        self.ser.write(b"\r\n\r\n")
        time.sleep(0.5)
        self.ser.reset_input_buffer()

    def status(self):
        """
        Returns GRBL's raw status report, e.g. '<Idle|MPos:0.000,0.000,0.000|FS:0,0>'.
        """
        self.ser.write(STATUS_QUERY)
        while True:
            line = self.ser.readline().decode(errors="replace").strip()
            if not line:
                return None
            if line.startswith("<"):
                return line

    def command(self, line):
        """
        Sends one line and returns the response lines up to and including 'ok'.
        Raises GrblError on 'error:N' or 'ALARM:N'.
        """
        self.ser.write((line.strip() + "\n").encode())
        out = []
        while True:
            resp = self.ser.readline().decode(errors="replace").strip()
            if not resp:
                raise GrblError(f"No response to {line!r}")
            if resp == "ok":
                return out
            if resp.startswith(("error", "ALARM")):
                raise GrblError(f"{resp} (in response to {line!r})")
            out.append(resp)

    def unlock(self):
        """
        Clears an alarm state ($X). Only do this once you know why it alarmed.
        """
        return self.command("$X")

    def stop(self):
        """
        Emergency stop: feed hold, laser off, then soft reset (which clears the
        planner buffer). Position is lost, so re-home or re-zero afterwards.
        """
        self.ser.write(FEED_HOLD)
        self.ser.write(SOFT_RESET)
        time.sleep(0.5)
        self.ser.reset_input_buffer()

    def send_file(self, filename, progress=True):
        """
        Streams a gcode file to the controller using GRBL character-counting, which
        keeps the RX buffer full so motion doesn't stall between lines. Blocks until
        every line is acknowledged. Ctrl-C / Jupyter's kernel interrupt calls stop(),
        which turns the laser off.
        """
        lines = read_gcode(filename)
        total = len(lines)
        pending = deque()  # byte length of each line sent but not yet acknowledged
        sent = acked = 0

        try:
            while acked < total:
                # Fill the controller's buffer as far as it will go
                while sent < total and sum(pending) + len(lines[sent]) + 1 < GRBL_RX_BUFFER:
                    self.ser.write((lines[sent] + "\n").encode())
                    pending.append(len(lines[sent]) + 1)
                    sent += 1

                resp = self.ser.readline().decode(errors="replace").strip()
                if resp == "ok":
                    pending.popleft()
                    acked += 1
                    if progress and (acked % 100 == 0 or acked == total):
                        print(f"\r{acked}/{total} lines", end="", flush=True)
                elif resp.startswith(("error", "ALARM")):
                    self.stop()
                    raise GrblError(f"{resp} at line {acked + 1}: {lines[acked]!r}")
                elif not resp and not pending:
                    break
        except KeyboardInterrupt:
            self.stop()
            print("\nInterrupted: stopped and reset controller.")
            raise
        if progress:
            print()
