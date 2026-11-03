"""
G-code agent: runs on the small computer plugged into the plotter (a Pi Zero, say) and lets
the main machine drive it over the network.

    python -m plotter_utils.agent            # or the `plotter-agent` command

It owns the serial port and streams each job to the controller locally, so a Wi-Fi hiccup
cannot stall a stroke. The main machine uses plotter_utils.RemoteConnection to talk to it
(set profile.agent_url). Only pyserial and the standard library are needed here.

Protocol: JSON over HTTP, every request carries the token in an X-Token header.

    GET  /status    {"job": {...}, "connected": bool, "grbl": "<Idle|...>" or null, ...}
    POST /job       {"lines": [...], "buffered": bool, "abort_lines": [...], "wait_s": n}
    POST /command   {"line": "G92 X0 Y0"}          one line, only while no job runs
    POST /stop      feed hold + reset; always allowed
    POST /unlock    sends $X

Dead-man switch: while a job runs the main machine polls /status. If nothing is heard for
`watchdog_s` seconds the agent stops the job (the reset turns the laser off) and then runs
the job's abort_lines (the client sends "$X" and a pen-up move).
"""
import argparse
import hmac
import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import serial

from .gcode_sender import GrblConnection, GrblError, GrblStopped, clean_gcode_line, resolve_port

__all__ = ["Agent", "serve_agent"]

PROTOCOL_VERSION = 1
MAX_BODY_BYTES = 64 * 1024 * 1024


class AgentError(Exception):
    """A request the agent refuses; `code` is the HTTP status to answer with."""
    def __init__(self, message, code=409):
        super().__init__(message)
        self.code = code


class Agent:
    def __init__(self, port="auto", baud=115200, token=None, watchdog_s=20.0, serial_timeout=1.0):
        self.port = "" if port in ("", "auto") else port
        self.baud = baud
        self.token = token or secrets.token_urlsafe(16)
        self.watchdog_s = watchdog_s
        self.serial_timeout = serial_timeout

        self.conn = None
        self.mutex = threading.Lock()   # serializes commands and job starts on the one serial port
        self.job = {"id": 0, "state": "idle", "done": 0, "total": 0, "message": ""}
        self.abort_lines = []
        self.last_seen = time.monotonic()
        self._thread = None
        self._quit = threading.Event()
        self.httpd = None

    # ---- serial connection ----------------------------------------------------

    def _connection(self):
        """The open connection, opening it first if needed. Raises AgentError if the plotter isn't there."""
        if self.conn is None:
            try:
                port, note = resolve_port(self.port or "/dev/ttyACM0")
                if note:
                    print(note, flush=True)
                self.conn = GrblConnection(port, self.baud, timeout=self.serial_timeout)
                print(f"Connected to the plotter on {port}", flush=True)
            except (OSError, serial.SerialException) as e:
                raise AgentError(f"No connection to the plotter: {e}", 503)
        return self.conn

    def _drop(self):
        """Forget a connection that failed (cable pulled, board reset); the next request reopens it."""
        conn, self.conn = self.conn, None
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    @property
    def busy(self):
        return self.job["state"] == "running"

    # ---- operations -----------------------------------------------------------

    def status(self):
        info = {"version": PROTOCOL_VERSION, "job": dict(self.job), "connected": self.conn is not None,
                "port": getattr(self.conn, "port", None), "watchdog_s": self.watchdog_s, "grbl": None}
        with self.mutex:
            if not self.busy:  # while a job runs the streamer owns the port
                try:
                    info["grbl"] = self._connection().status()  # (re)opens the port if the cable was replugged
                    info["connected"], info["port"] = True, self.conn.port
                except AgentError as e:
                    info["connected"], info["error"] = False, str(e)
                except (OSError, serial.SerialException):
                    self._drop()
                    info["connected"] = False
        return info

    def command(self, line):
        with self.mutex:
            if self.busy:
                raise AgentError("A job is running; only /stop and /status are available")
            conn = self._connection()
            try:
                return conn.command(line)
            except GrblError as e:
                raise AgentError(str(e), 422)
            except (OSError, serial.SerialException) as e:
                self._drop()
                raise AgentError(f"Serial connection lost: {e}", 503)

    def start_job(self, lines, buffered=True, abort_lines=None, wait_s=0.0):
        lines = [l for l in (clean_gcode_line(str(l)) for l in lines) if l]
        if not lines:
            raise AgentError("The job has no G-code lines", 400)
        with self.mutex:
            if self.busy:
                raise AgentError("A job is already running")
            conn = self._connection()
            self.abort_lines = [str(l) for l in (abort_lines or [])]
            self.job = {"id": self.job["id"] + 1, "state": "running", "done": 0, "total": len(lines), "message": ""}
            self._thread = threading.Thread(target=self._run_job, args=(conn, lines, buffered), daemon=True)
            self._thread.start()
        if wait_s > 0:  # short jobs (a pen move) finish inside the request, which feels instant
            deadline = time.monotonic() + wait_s
            while self._thread.is_alive() and time.monotonic() < deadline:
                self._thread.join(0.1)
                self.last_seen = time.monotonic()  # the client is waiting on this open request, so it is still there
        return dict(self.job)

    def _run_job(self, conn, lines, buffered):
        state, message = "done", ""
        try:
            conn.send_lines(lines, buffered=buffered, on_progress=lambda d, t: self.job.__setitem__("done", d))
        except GrblStopped:
            state, message = "stopped", self.job.get("watchdog_message", "Stopped")
        except GrblError as e:
            state, message = "error", str(e)
        except (OSError, serial.SerialException) as e:
            state, message = "error", f"Serial connection lost: {e}"
            self._drop()
        except Exception as e:  # never leave a job "running" forever
            state, message = "error", f"{type(e).__name__}: {e}"
        self.job.update(state=state, message=message)

    def stop(self):
        """Emergency stop: feed hold + soft reset (the laser goes off). Allowed at any time."""
        conn = self.conn
        if conn is None:
            raise AgentError("No connection to the plotter", 503)
        try:
            conn.stop()
        except (OSError, serial.SerialException) as e:
            self._drop()
            raise AgentError(f"Serial connection lost while stopping: {e}", 503)

    # ---- dead-man switch ------------------------------------------------------

    def _watchdog(self):
        while not self._quit.wait(1.0):
            if self.busy and self.watchdog_s > 0 and time.monotonic() - self.last_seen > self.watchdog_s:
                self._trip()

    def _trip(self):
        """Lost contact mid-job: stop, then run the client's abort lines (unlock, raise the pen)."""
        self.job["watchdog_message"] = (f"Lost contact with the main machine for {self.watchdog_s:g} s: "
                                        f"job stopped and pen lifted")
        print(self.job["watchdog_message"], flush=True)
        try:
            self.stop()
        except AgentError:
            return
        if self._thread:
            self._thread.join(5)
        with self.mutex:
            for line in self.abort_lines:
                try:
                    self.conn.command(line)
                except Exception as e:
                    print(f"Abort line {line!r} failed: {e}", flush=True)
                    break
        self.last_seen = time.monotonic()

    # ---- HTTP -----------------------------------------------------------------

    def start(self, host="0.0.0.0", port=8765):
        agent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                if not hmac.compare_digest(self.headers.get("X-Token", ""), agent.token):
                    return self._send(403, {"error": "bad token"})
                agent.last_seen = time.monotonic()  # every authenticated request counts as "still there"
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_BODY_BYTES:
                        raise AgentError("Request too large", 413)
                    try:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except ValueError:
                        raise AgentError("Request body is not valid JSON", 400)
                    if method == "GET" and self.path == "/status":
                        return self._send(200, agent.status())
                    if method == "POST":
                        if self.path == "/job":
                            return self._send(200, {"job": agent.start_job(
                                body.get("lines", []), bool(body.get("buffered", True)),
                                body.get("abort_lines"), float(body.get("wait_s", 0)))})
                        if self.path == "/command":
                            return self._send(200, {"response": agent.command(str(body.get("line", "")))})
                        if self.path == "/unlock":
                            return self._send(200, {"response": agent.command("$X")})
                        if self.path == "/stop":
                            agent.stop()
                            return self._send(200, {"stopped": True})
                    self._send(404, {"error": "not found"})
                except AgentError as e:
                    self._send(e.code, {"error": str(e)})
                except Exception as e:
                    self._send(500, {"error": f"{type(e).__name__}: {e}"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        return self

    @property
    def listen_port(self):
        return self.httpd.server_address[1]

    def shutdown(self):
        """Stops serving. A job still running is stopped first so the laser can't stay on."""
        self._quit.set()
        if self.busy and self.conn is not None:
            try:
                self.conn.stop()
            except Exception:
                pass
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self._drop()


def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _load_token(token, token_file):
    """--token, else $PLOTTER_AGENT_TOKEN, else the token file (created on first run)."""
    token = token or os.environ.get("PLOTTER_AGENT_TOKEN")
    if token:
        return token, "given on the command line / environment"
    path = os.path.expanduser(token_file)
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip(), path
    token = secrets.token_urlsafe(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # owner-only: it is the key to the machine
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token, path + " (just created)"


def serve_agent(port="auto", baud=115200, host="0.0.0.0", listen_port=8765, token=None, watchdog_s=20.0,
                serial_timeout=1.0):
    """
    Starts an Agent on a background thread and returns it (call .shutdown() to stop). The
    command line (python -m plotter_utils.agent) is this plus a wait loop and a token file.
    """
    return Agent(port, baud, token, watchdog_s, serial_timeout).start(host, listen_port)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="plotter-agent", description="G-code agent for a plotter on this machine")
    ap.add_argument("--port", default="auto", help="serial device, or 'auto' for the only USB serial device (default)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on")
    ap.add_argument("--listen-port", type=int, default=8765)
    ap.add_argument("--token", help="shared secret; default: $PLOTTER_AGENT_TOKEN or the token file")
    ap.add_argument("--token-file", default="~/.plotter_agent_token")
    ap.add_argument("--watchdog", type=float, default=20.0,
                    help="seconds without contact before a running job is stopped (0 turns it off)")
    ap.add_argument("--serial-timeout", type=float, default=1.0)
    args = ap.parse_args(argv)

    token, where = _load_token(args.token, args.token_file)
    agent = serve_agent(args.port, args.baud, args.host, args.listen_port, token, args.watchdog, args.serial_timeout)
    print(f"plotter-agent listening on http://{_lan_ip()}:{agent.listen_port}", flush=True)
    if "just created" in where:  # shown once; after that it stays in the file instead of the system log
        print(f"  token: {token}   (new; saved in {where.replace(' (just created)', '')})", flush=True)
    else:
        print(f"  token: read from {where}", flush=True)
    print(f"  watchdog: {'off' if args.watchdog <= 0 else f'{args.watchdog:g} s'}", flush=True)
    try:
        agent._connection()  # open the port now so problems show up at startup; retried per request if it fails
    except AgentError as e:
        print(f"  warning: {e}", flush=True)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    try:
        threading.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        agent.shutdown()


if __name__ == "__main__":
    main()
