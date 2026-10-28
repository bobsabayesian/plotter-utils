import os
import re
import time
from collections import deque

import serial
from serial.tools import list_ports

__all__ = [
    "GrblConnection", "SimulatedConnection", "connect", "resolve_port", "GrblError", "GrblStopped",
    "explain_grbl", "find_ports", "read_gcode", "check_gcode",
    "clean_gcode_line", "DEFAULT_BAUD",
]

MAX_SILENT_READS = 60  # consecutive empty reads (1 s each) before giving up
GRBL_RX_BUFFER = 127  # bytes in GRBL's serial RX buffer
DEFAULT_BAUD = 115200

# GRBL real-time commands: single bytes, not newline-terminated, bypass the buffer
FEED_HOLD = b"!"
CYCLE_START = b"~"
SOFT_RESET = b"\x18"
STATUS_QUERY = b"?"


class GrblError(Exception):
    pass


class GrblStopped(GrblError):
    """The job was stopped on purpose (stop() / the STOP button), not by a controller error."""


# What GRBL's codes mean, for the ones you are likely to meet. Other codes pass through as-is.
GRBL_CODES = {
    "ALARM:1": "hard limit triggered",
    "ALARM:2": "target is outside the machine's soft limits",
    "ALARM:3": "reset while the machine was moving, so its position may be lost",
    "ALARM:4": "probe failed",
    "ALARM:5": "probe failed",
    "ALARM:6": "homing failed",
    "ALARM:7": "homing failed",
    "ALARM:8": "homing failed",
    "ALARM:9": "homing failed",
    "error:1": "G-code word has no command letter",
    "error:2": "G-code number is badly formed",
    "error:3": "unrecognized $ setting command",
    "error:8": "command needs the machine to be idle",
    "error:9": "locked: the controller is in an alarm state and refuses G-code until unlocked ($X)",
    "error:20": "unsupported or invalid G-code command",
    "error:22": "no feed rate set for a move that needs one",
    "error:33": "target position is invalid for this move",
}


def explain_grbl(response):
    """
    Adds a plain-language meaning to a GRBL 'error:N' / 'ALARM:N' response.
    """
    meaning = GRBL_CODES.get(response.strip())
    return f"{response.strip()} ({meaning})" if meaning else response.strip()


def find_ports():
    """
    Lists serial ports that look like USB devices, as (device, description) pairs.
    """
    return [(p.device, p.description) for p in list_ports.comports()
            if "USB" in (p.hwid or "") or "ACM" in p.device or "ttyUSB" in p.device]


def resolve_port(port):
    """
    Returns (device, note) for the serial port to open. If `port` exists it is used as
    given (note is ""). If it doesn't (the plotter re-enumerated as /dev/ttyACM1 after a
    replug, say) and exactly one USB serial device is attached, that one is used and
    the note says so. It never guesses between several: that raises with the list,
    because opening the wrong device could drive the wrong hardware.
    """
    found = find_ports()
    if os.path.exists(port) or port in [d for d, _ in found]:
        return port, ""
    if len(found) == 1:
        return found[0][0], f"{port} not found; using the only USB serial device, {found[0][0]} ({found[0][1]})."
    if not found:
        raise FileNotFoundError(
            f"No serial port at {port} and no USB serial devices detected. Check the plotter's power and "
            f"USB cable (use a data cable), and that this machine is the one it is plugged into."
        )
    options = ", ".join(f"{d} ({desc})" for d, desc in found)
    raise FileNotFoundError(f"No serial port at {port}, and several candidates exist: {options}. Set profile.port to one.")


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
        self.port = port
        self.ser = serial.Serial(port, baud, timeout=timeout)
        self._stopped = False
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
                raise GrblError(f"{explain_grbl(resp)} (in response to {line!r})")
            out.append(resp)

    def unlock(self):
        """
        Clears an alarm state ($X). Only do this once you know why it alarmed.
        """
        return self.command("$X")

    def stop(self):
        """
        Emergency stop: feed hold, laser off, then soft reset (which clears the
        planner buffer). Position is lost, so re-zero afterwards. Safe to call from
        another thread; a running send_lines() will raise GrblStopped. Resetting
        mid-move often leaves GRBL in an alarm state ("ALARM:3"); the next G-code is
        refused with error:9 until unlock() ($X).
        """
        self._stopped = True
        self.ser.write(FEED_HOLD)
        self.ser.write(SOFT_RESET)
        time.sleep(0.5)
        self.ser.reset_input_buffer()

    def set_zero(self):
        """
        Treats the current head position as X0 Y0 (the iDraw has no homing).
        """
        return self.command("G92 X0 Y0")

    def pen_up(self, z=0):
        return self.command(f"G00 Z{z:g}")

    def pen_down(self, z=6):
        return self.command(f"G00 Z{z:g}")

    def send_lines(self, lines, buffered=True, on_progress=None):
        """
        Sends G-code lines and blocks until every one is acknowledged.

        buffered=True streams with GRBL character-counting, keeping the controller's
        RX buffer full so motion doesn't stall. buffered=False sends one line at a
        time and waits for 'ok' -- slower, but the safest choice on firmware that
        doesn't behave like stock GRBL. on_progress(done, total) is called as lines
        are acknowledged. KeyboardInterrupt (Jupyter's kernel interrupt) calls stop().
        """
        lines = [l for l in (clean_gcode_line(l) for l in lines) if l]
        total = len(lines)
        self._stopped = False
        pending = deque()  # byte length of each line sent but not yet acknowledged
        sent = acked = silent = 0

        try:
            while acked < total:
                if self._stopped:
                    raise GrblStopped("Stopped")
                # Fill the controller's buffer; unbuffered sends one line at a time
                while sent < total and (buffered or not pending) and sum(pending) + len(lines[sent]) + 1 < GRBL_RX_BUFFER:
                    self.ser.write((lines[sent] + "\n").encode())
                    pending.append(len(lines[sent]) + 1)
                    sent += 1

                resp = self.ser.readline().decode(errors="replace").strip()
                if self._stopped:
                    # Stopped while waiting: whatever came back (typically the ALARM:3 or the
                    # startup banner that a reset produces) is a result of stopping, not a fault
                    raise GrblStopped("Stopped")
                if resp == "ok":
                    silent = 0
                    pending.popleft()
                    acked += 1
                    if on_progress:
                        on_progress(acked, total)
                elif resp.startswith(("error", "ALARM")):
                    self.stop()
                    raise GrblError(f"{explain_grbl(resp)} at line {acked + 1}: {lines[acked]!r}")
                elif not resp:
                    # Long moves legitimately go quiet; give up only after a long silence
                    silent += 1
                    if silent > MAX_SILENT_READS:
                        raise GrblError(f"No response from controller at line {acked + 1}: {lines[acked]!r}")
        except KeyboardInterrupt:
            self.stop()
            raise

    def send_file(self, filename, buffered=True, on_progress=None):
        """
        Streams a gcode file to the controller. See send_lines().
        """
        self.send_lines(read_gcode(filename), buffered=buffered, on_progress=on_progress)


class SimulatedConnection:
    """
    Stands in for GrblConnection with no hardware: same methods, nothing is opened or
    moved. Every line "sent" is kept in .sent so you can inspect what would have gone
    to the machine.
    """
    simulated = True

    def __init__(self):
        self.sent = []
        self._stopped = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        pass

    def status(self):
        return "<Idle|MPos:0.000,0.000,0.000|FS:0,0> (simulated)"

    def command(self, line):
        self.sent.append(line.strip())
        return []

    def unlock(self):
        return self.command("$X")

    def stop(self):
        self._stopped = True

    def set_zero(self):
        return self.command("G92 X0 Y0")

    def pen_up(self, z=0):
        return self.command(f"G00 Z{z:g}")

    def pen_down(self, z=6):
        return self.command(f"G00 Z{z:g}")

    def send_lines(self, lines, buffered=True, on_progress=None):
        self._stopped = False
        lines = [l for l in (clean_gcode_line(l) for l in lines) if l]
        for i, line in enumerate(lines, 1):
            if self._stopped:
                raise GrblStopped("Stopped")
            self.sent.append(line)
            if on_progress:
                on_progress(i, len(lines))

    def send_file(self, filename, buffered=True, on_progress=None):
        self.send_lines(read_gcode(filename), buffered=buffered, on_progress=on_progress)


def connect(profile):
    """
    The one place a connection is made. With profile.test_mode (the default) it returns
    a SimulatedConnection and never touches a serial port; otherwise it opens the
    real GrblConnection on profile.port. If that port doesn't exist but exactly one USB
    serial device is attached, it uses that one and prints which (see resolve_port).
    """
    if profile.test_mode:
        return SimulatedConnection()
    port, note = resolve_port(profile.port)
    if note:
        print(note)
    return GrblConnection(port, profile.baud)
