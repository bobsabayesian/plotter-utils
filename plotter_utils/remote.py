import collections
import hmac
import io
import json
import os
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import vpype

from .drawing import (page_size_label, page_size_string, registration_ticks_svg, rotate_document,
                      svg_page_size_mm)
from .gcode import document_to_gcode, estimate_plot
from .gcode_sender import connect, read_gcode
from .job import wait_with_cancel
from .layers import extract_layer, layer_summaries, read_document
from .optimize import optimize_document

__all__ = ["RemoteControl", "serve_remote", "qr_svg"]

# Servers started in this process. setdefault keeps the list across %autoreload, so a
# re-run cell can still find and stop the server it started last time.
_ACTIVE = globals().setdefault("_ACTIVE", [])

MAX_JOG_MM = 100
Z_RANGE = (0, 9)


def qr_svg(text, scale=6):
    """
    Returns a QR code for `text` as an SVG string (black on white with a quiet border,
    so it scans in dark mode too).
    """
    import io
    import segno
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=scale, border=4,
                                     dark="#000", light="#fff", xmldecl=False, svgns=True)
    return buf.getvalue().decode()


def _in_notebook():
    try:
        from IPython import get_ipython
        return get_ipython() is not None
    except ImportError:
        return False


class RemoteError(Exception):
    """Raised for requests the server refuses; the message is shown on the page."""


class RemoteControl:
    """
    A small web UI (for a tablet or phone) that drives the plotter: calibrate pen
    up/down, jog and set zero, draw registration marks, and plot one chosen file.

    Created by serve_remote(); the HTTP server runs on a background thread so the
    Jupyter cell returns immediately. All hardware access goes through one
    connection, one job at a time; only /api/stop is allowed while a job runs.
    """

    def __init__(self, profile, connection, file=None, optimize=True, tick_length="2mm",
                 allow_laser=False, save_path=None):
        self.profile = profile
        self.conn = connection
        self.file = file
        self.optimize = optimize
        self.tick_mm = vpype.convert_length(tick_length) / vpype.convert_length("1mm")
        self.allow_laser = allow_laser
        self.save_path = save_path

        self.token = secrets.token_urlsafe(8)
        self.lock = threading.Lock()  # held while a job or command is using the machine
        self.pen_z = profile.pen_up_z
        self.pos = None  # estimated (x, y) mm from zero; None until set_zero
        self.busy = None
        self.progress = (0, 0)
        self.message = "Ready."
        self.httpd = None
        self.rotation = 0  # degrees clockwise: 0, 90, 180, 270
        self.paper = self._paper_from_file()
        self.layers = []         # LayerSummary list for the loaded SVG
        self.layer_sel = "all"   # a layer id, or "all"
        self._load_layers()
        self.requests = collections.deque(maxlen=50)  # recent (time, client ip, path, user agent)
        self.cancel = threading.Event()
        self.counting = False  # True during the start delay, before the machine has moved

    # ---- state -------------------------------------------------------------

    def state(self):
        p = self.profile
        return {
            "test_mode": p.test_mode, "busy": self.busy, "progress": self.progress,
            "message": self.message, "pen_z": self.pen_z, "pen_up_z": p.pen_up_z,
            "pen_down_z": p.pen_down_z, "pen_settle_s": p.pen_settle_s, "pos": self.pos, "zero_set": self.pos is not None,
            "file": os.path.basename(self.file) if self.file else None,
            "has_preview": bool(self.file and self.file.lower().endswith(".svg")),
            "paper_size": page_size_label(*self.paper) if self.paper else p.paper_size,
            "paper_source": "file" if self.paper else "profile", "tick_mm": self.tick_mm,
            "layers": [{"id": l.id, "name": l.name, "pen": l.pen, "hex": l.hex_color, "paths": l.paths}
                       for l in self.layers],
            "layer": self.layer_sel, "rotation": self.rotation,
            "z_range": Z_RANGE, "start_delay_s": self.profile.start_delay_s,
        }

    def _paper_from_file(self):
        """(width_mm, height_mm) of the loaded SVG's page as it will sit on the machine
        (after any rotation), or None (raw G-code, or no declared size)."""
        if self.file and self.file.lower().endswith(".svg"):
            size = svg_page_size_mm(self.file)
            if size and self.rotation in (90, 270):
                size = (size[1], size[0])
            return size
        return None

    def paper_string(self):
        """The paper size registration marks use: the file's, else profile.paper_size."""
        return page_size_string(*self.paper) if self.paper else self.profile.paper_size

    def set_file(self, path):
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self.file = path
        self.rotation = 0
        self.paper = self._paper_from_file()
        self._load_layers()
        self.message = f"File set: {os.path.basename(path)}"

    def _load_layers(self):
        """Lists the SVG's layers for the picker. Several layers start on the first one,
        so a stray tap doesn't draw every color with one pen."""
        self.layers = []
        if self.file and self.file.lower().endswith(".svg"):
            self.layers = layer_summaries(read_document(self.file), self.profile.pens)
        self.layer_sel = self.layers[0].id if len(self.layers) > 1 else "all"

    def preview_svg(self, layer="all"):
        """
        SVG bytes for the page preview: the whole file, or just one layer drawn at its
        place on the page. Raises KeyError for an unknown layer.
        """
        if (layer == "all" or not self.layers) and self.rotation == 0:
            with open(self.file, "rb") as f:
                return f.read()
        doc = self._view_document(layer)
        buf = io.StringIO()
        vpype.write_svg(buf, doc, page_size=doc.page_size)
        return buf.getvalue().encode()

    def _view_document(self, layer):
        """The loaded SVG as it will plot: just `layer` (or all), then rotated.
        Raises KeyError for an unknown layer."""
        doc = read_document(self.file)
        if layer != "all" and self.layers:
            doc = extract_layer(doc, layer)
        return rotate_document(doc, self.rotation)

    def set_rotation(self, degrees):
        if self.busy:
            raise RemoteError(f"Busy: {self.busy}")
        if not (self.file and self.file.lower().endswith(".svg")):
            raise RemoteError("Rotation applies to SVG files only")
        degrees = int(degrees)
        if degrees not in (0, 90, 180, 270):
            raise RemoteError("Rotation must be 0, 90, 180 or 270")
        self.rotation = degrees
        self.paper = self._paper_from_file()
        self.message = f"Rotated {degrees}° clockwise." if degrees else "Not rotated."

    def select_layer(self, layer):
        if self.busy:
            raise RemoteError(f"Busy: {self.busy}")
        if layer == "all":
            self.layer_sel = "all"
        elif any(l.id == layer for l in self.layers):
            self.layer_sel = layer
        else:
            raise RemoteError("No such layer")
        self.message = "All layers (one pen)" if layer == "all" else \
            "Layer: {name}. Load the {pen} pen.".format(**next(vars(l) for l in self.layers if l.id == layer))

    def set_tick(self, mm):
        mm = float(mm)
        if not 0.5 <= mm <= 50:
            raise RemoteError("Tick size must be 0.5-50 mm")
        self.tick_mm = mm

    # ---- actions (each returns after updating self.message) ----------------

    def _claim(self, what):
        if not self.lock.acquire(blocking=False):
            raise RemoteError(f"Busy: {self.busy or 'another command'} in progress")
        self.busy = what

    def _release(self):
        self.busy = None
        self.lock.release()

    def _run(self, what, lines, buffered=False):
        """Claims the machine, sends lines, releases. Raises on failure."""
        self._claim(what)
        try:
            self.progress = (0, len(lines))
            self.conn.send_lines(lines, buffered=buffered, on_progress=lambda d, t: setattr(self, "progress", (d, t)))
        finally:
            self._release()

    def move_pen(self, z):
        lo, hi = Z_RANGE
        z = float(z)
        if not lo <= z <= hi:
            raise RemoteError(f"Z must be {lo}-{hi}")
        self._run("pen", ["G90", f"G00 Z{z:g}"])
        self.pen_z = z
        self.message = f"Pen at Z{z:g}"

    def save_pen_height(self, which):
        if which not in ("up", "down"):
            raise RemoteError("which must be 'up' or 'down'")
        setattr(self.profile, f"pen_{which}_z", self.pen_z)
        if self.save_path:
            self.profile.save(self.save_path)
        self.message = f"Pen {which} height = Z{self.pen_z:g}" + (" (saved)" if self.save_path else " (not saved to disk)")

    def set_settle(self, seconds):
        """Dwell after every pen move, so the pen has landed/lifted before XY motion continues."""
        seconds = float(seconds)
        if not 0 <= seconds <= 5:
            raise RemoteError("Settle time must be 0-5 s")
        self.profile.pen_settle_s = seconds
        if self.save_path:
            self.profile.save(self.save_path)
        self.message = f"Pen settle time = {seconds:g} s" + (" (saved)" if self.save_path else " (not saved to disk)")

    def draw_test_line(self, mm=10):
        """Lowers the pen, draws a short line along +X from here, raises it. For tuning
        pen-down height and settle time."""
        mm = float(mm)
        p = self.profile
        if self.pen_z != p.pen_up_z:
            raise RemoteError("Raise the pen first")
        if not 1 <= mm <= 50:
            raise RemoteError("Test line must be 1-50 mm")
        if self.pos is not None and p.bed_size_mm and self.pos[0] + mm > p.bed_size_mm[0]:
            raise RemoteError("The test line would leave the bed")
        dwell = [f"G04 P{p.pen_settle_s:g}"] if p.pen_settle_s else []
        self._run("test line", ["G90", f"G00 Z{p.pen_down_z:g}", *dwell,
                                "G91", f"G01 X{mm:g} F{p.draw_feed:g}", "G90",
                                f"G00 Z{p.pen_up_z:g}", *dwell])
        if self.pos is not None:
            self.pos = (self.pos[0] + mm, self.pos[1])
        self.message = f"Drew a {mm:g} mm test line."

    def jog(self, dx, dy):
        dx, dy = float(dx), float(dy)
        if max(abs(dx), abs(dy)) > MAX_JOG_MM:
            raise RemoteError(f"Jog limited to {MAX_JOG_MM} mm")
        if self.pen_z != self.profile.pen_up_z:
            raise RemoteError("Raise the pen before moving")
        bed = self.profile.bed_size_mm
        if self.pos is not None and bed:
            nx, ny = self.pos[0] + dx, self.pos[1] + dy
            if not (0 <= nx <= bed[0] and 0 <= ny <= bed[1]):
                raise RemoteError(f"Would leave the bed {bed}")
        self._run("jog", ["G91", f"G01 X{dx:g} Y{dy:g} F{min(self.profile.draw_feed, 2000):g}", "G90"])
        if self.pos is not None:
            self.pos = (self.pos[0] + dx, self.pos[1] + dy)
        self.message = f"Jogged {dx:g}, {dy:g} mm"

    def set_zero(self):
        self._run("zero", ["G92 X0 Y0"])
        self.pos = (0.0, 0.0)
        self.message = "Zero set here."

    def go_to_zero(self):
        """Moves the pen (raised) back to the X0 Y0 you set, along a straight line."""
        if self.pos is None:
            raise RemoteError("Set zero first; there is no zero point to return to")
        if self.pen_z != self.profile.pen_up_z:
            raise RemoteError("Raise the pen before moving")
        self._run("go to zero", ["G90", f"G01 X0 Y0 F{min(self.profile.draw_feed, 2000):g}"])
        self.pos = (0.0, 0.0)
        self.message = "Back at zero."

    def _require_ready(self):
        if self.pos is None:
            raise RemoteError("Set zero first (jog the pen to the paper's lower-left corner)")
        if self.pen_z != self.profile.pen_up_z:
            raise RemoteError("Raise the pen first")

    def start_registration(self, delay=None, tick_mm=None):
        self._require_ready()
        if tick_mm is not None:
            self.set_tick(tick_mm)
        # Same route as any SVG (SVG text -> Document -> G-code), but held in memory: no file is written
        doc = read_document(io.StringIO(registration_ticks_svg(self.paper_string(), f"{self.tick_mm:g}mm")))
        self._start_job("registration marks", document_to_gcode(doc, self.profile), delay=delay)

    def start_plot(self, delay=None):
        self._require_ready()
        if not self.file:
            raise RemoteError("No file chosen")
        name = os.path.basename(self.file)
        advance = None
        if self.file.lower().endswith(".svg"):
            try:
                doc = self._view_document(self.layer_sel)
            except KeyError:
                raise RemoteError("That layer is no longer in the file; set the file again")
            if self.layer_sel != "all":
                layer = next(l for l in self.layers if l.id == self.layer_sel)
                name = f"{name} / {layer.name} ({layer.pen})"
                advance = self._next_layer_after(layer.id)
            if self.rotation:
                name += f" (rotated {self.rotation}°)"
            if self.optimize:
                doc = optimize_document(doc)
            lines = document_to_gcode(doc, self.profile)
            est = estimate_plot(doc, self.profile)
            note = f"about {est['seconds'] / 60:.1f} min"
        else:
            lines = read_gcode(self.file)
            if not self.allow_laser and any(re.search(r"\bM0?[34]\b", l, re.I) for l in lines):
                raise RemoteError("File uses M3/M4 (laser/spindle); start serve_remote(allow_laser=True) to permit")
            note = f"{len(lines)} lines"
        self._start_job(name, lines, note, delay, on_done=advance)

    def _next_layer_after(self, layer_id):
        """A callback that moves the picker to the following layer once this one finishes."""
        ids = [l.id for l in self.layers]
        if layer_id not in ids or ids.index(layer_id) == len(ids) - 1:
            return None
        nxt = next(l for l in self.layers if l.id == ids[ids.index(layer_id) + 1])

        def advance():
            self.layer_sel = nxt.id
            self.message += f" Next layer: {nxt.name}. Swap to the {nxt.pen} pen."
        return advance

    def _start_job(self, name, lines, note="", delay=None, on_done=None):
        delay = self.profile.start_delay_s if delay is None else float(delay)
        if not 0 <= delay <= 600:
            raise RemoteError("Start delay must be 0-600 s")
        self._claim(name)  # claimed here so a second tap is rejected immediately
        self.progress = (0, len(lines))
        self.message = f"Plotting {name} {note}".strip()
        self.cancel.clear()

        def work():
            try:
                if delay > 0:
                    self.counting = True
                    ok = wait_with_cancel(delay, self.cancel, lambda n: setattr(
                        self, "message", f"{name} starts in {n} s. Press STOP to cancel."))
                    self.counting = False
                    if not ok:
                        self.message = f"Cancelled {name} before it started."
                        return
                    self.message = f"Plotting {name} {note}".strip()
                self.conn.send_lines(lines, buffered=False, on_progress=lambda d, t: setattr(self, "progress", (d, t)))
                self.pen_z = self.profile.pen_up_z
                self.message = f"Done: {name}."
                if on_done:
                    on_done()
            except Exception as e:  # includes GrblError from stop/alarm
                self.pos, self.pen_z = None, self.profile.pen_up_z
                self.message = f"Stopped: {e}. Position lost; set zero again."
            finally:
                self._release()

        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        """Emergency stop. Allowed any time, even while a job holds the lock."""
        self.cancel.set()
        if self.counting:  # still in the start delay: the machine hasn't moved, so nothing is lost
            self.message = "Cancelled before the plot started."
            return
        self.conn.stop()
        self.pos = None
        self.message = "STOPPED. Position lost; set zero again."

    # ---- server ------------------------------------------------------------

    def start(self, host="0.0.0.0", port=8000):
        remote = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _log(self):
                ua = self.headers.get("User-Agent", "")
                remote.requests.append((time.strftime("%H:%M:%S"), self.client_address[0],
                                        urlparse(self.path).path, ua[:70]))

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self):
                q = parse_qs(urlparse(self.path).query)
                given = self.headers.get("X-Token") or (q.get("token") or [""])[0]
                return hmac.compare_digest(given, remote.token)

            def do_GET(self):
                self._log()
                path = urlparse(self.path).path
                if not self._authorized():
                    return self._send(403, {"error": "bad token"})
                if path == "/":
                    return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                if path == "/api/state":
                    return self._send(200, remote.state())
                if path == "/api/file.svg" and remote.file and remote.file.lower().endswith(".svg"):
                    layer = (parse_qs(urlparse(self.path).query).get("layer") or ["all"])[0]
                    try:
                        svg = remote.preview_svg("all" if layer == "all" else int(layer))
                    except (KeyError, ValueError):
                        return self._send(404, {"error": "no such layer"})
                    return self._send(200, svg, "image/svg+xml")
                self._send(404, {"error": "not found"})

            def do_POST(self):
                self._log()
                if not self._authorized():
                    return self._send(403, {"error": "bad token"})
                path = urlparse(self.path).path
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                routes = {
                    "/api/pen": lambda: remote.move_pen(body["z"]),
                    "/api/save_height": lambda: remote.save_pen_height(body["which"]),
                    "/api/jog": lambda: remote.jog(body["dx"], body["dy"]),
                    "/api/settle": lambda: remote.set_settle(body["seconds"]),
                    "/api/test_line": lambda: remote.draw_test_line(body.get("mm", 10)),
                    "/api/zero": remote.set_zero,
                    "/api/goto_zero": remote.go_to_zero,
                    "/api/registration": lambda: remote.start_registration(body.get("delay"), body.get("tick_mm")),
                    "/api/layer": lambda: remote.select_layer(body["layer"]),
                    "/api/rotate": lambda: remote.set_rotation(body["degrees"]),
                    "/api/plot": lambda: remote.start_plot(body.get("delay")),
                    "/api/stop": remote.stop,
                }
                if path not in routes:
                    return self._send(404, {"error": "not found"})
                try:
                    routes[path]()
                    self._send(200, remote.state())
                except (RemoteError, ValueError) as e:  # ValueError: bounds check in gcode generation
                    remote.message = str(e)
                    self._send(409, {"error": str(e)})
                except Exception as e:
                    remote.message = f"Error: {e}"
                    self._send(500, {"error": str(e)})

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    @property
    def port(self):
        return self.httpd.server_address[1]

    def url(self):
        return f"http://{_lan_ip()}:{self.port}/?token={self.token}"

    def qr(self, by_name=False):
        """
        Shows the page link as a QR code in a notebook, so a tablet can scan it with
        its camera instead of you typing the link. Does nothing outside a notebook.
        """
        if not _in_notebook():
            return
        from IPython.display import SVG, display
        display(SVG(qr_svg(self.url_by_name() if by_name else self.url())))

    def seen(self, n=15, quiet_polls=True):
        """
        Prints the most recent requests that reached this server: time, client IP, path,
        browser. Use it to check whether a device (e.g. the iPad) is reaching the plotter
        at all. The page's once-a-second status polls are hidden unless quiet_polls=False.
        """
        rows = [r for r in self.requests if not (quiet_polls and r[2] == "/api/state")]
        if not rows:
            print("No requests have reached this server yet.")
        for t, ip, path, ua in rows[-n:]:
            print(f"{t}  {ip:<16} {path:<18} {ua}")

    def url_by_name(self):
        """Same link using <hostname>.local, which survives a changing IP (mDNS; works on Pi OS)."""
        name = socket.gethostname().split(".")[0]
        return f"http://{name}.local:{self.port}/?token={self.token}"

    def shutdown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets sent; just picks the outbound interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def serve_remote(profile, file=None, connection=None, host="0.0.0.0", port=8000, optimize=True,
                 tick_length="2mm", allow_laser=False, save_path=None, show_qr=True):
    """
    Starts the tablet control page on a background thread and returns the
    RemoteControl (call .shutdown() to stop it, .set_file(path) to change the file).

    With profile.test_mode on (the default) it uses a simulated connection, so you can
    try the whole page with no plotter. The URL contains a random token, which is the
    only access control: anyone on your network who has the link can move the machine,
    and traffic is plain HTTP. Use it on a trusted home network only.
    """
    conn = connection if connection is not None else connect(profile)
    if profile.test_mode and not getattr(conn, "simulated", False):
        raise RuntimeError("profile.test_mode is on but a real connection was supplied")

    # Re-running the cell: stop the server from last time so its port is free again
    while _ACTIVE:
        _ACTIVE.pop().shutdown()

    remote = RemoteControl(profile, conn, file, optimize, tick_length, allow_laser, save_path)
    try:
        remote.start(host, port)
    except OSError as e:
        raise OSError(
            f"Port {port} is not available ({e.strerror}). Something outside this notebook is using it: "
            f"try serve_remote(..., port={port + 1}) or port=0 (pick any free port), "
            f"or find it with:  ss -ltnp | grep {port}"
        ) from e
    _ACTIVE.append(remote)
    print(f"Plotter remote ({'TEST MODE' if profile.test_mode else 'LIVE'}): {remote.url()}")
    print(f"  or by name:  {remote.url_by_name()}")
    if show_qr:
        remote.qr()  # scan with the tablet camera; a no-op outside a notebook
    print("  If the page just spins: open the link without ?token=... on the tablet; instant "
          "'bad token' text = network is fine, a spinner = the tablet can't reach this machine.")
    return remote


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Plotter Remote</title>
<style>
:root{--bg:#f4f4f2;--card:#fff;--ink:#1c1c1e;--mute:#6b6b70;--line:#d9d9de;--accent:#0a64d8;--warn:#b25b00;--stop:#c8261c;--ok:#1a7f37}
@media (prefers-color-scheme:dark){:root{--bg:#111113;--card:#1c1c1f;--ink:#f2f2f4;--mute:#9a9aa2;--line:#34343a;--accent:#4c9bff;--warn:#f0a35a;--stop:#ff5a4f;--ok:#4cc26d}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.4 -apple-system,system-ui,sans-serif;padding:0 16px 120px}
header{display:flex;gap:10px;align-items:center;padding:14px 0 6px}
h1{font-size:20px;margin:0;flex:1}
.badge{font-size:13px;font-weight:600;padding:3px 10px;border-radius:99px;border:1.5px solid}
.test{color:var(--warn)}.live{color:var(--ok)}
section{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin:12px 0}
h2{font-size:14px;letter-spacing:.04em;text-transform:uppercase;color:var(--mute);margin:0 0 10px}
button{font:inherit;min-height:48px;padding:0 16px;border-radius:11px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
button:active{filter:brightness(.9)}button:disabled{opacity:.4}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.row{display:flex;gap:10px;flex-wrap:wrap}.row>*{flex:1}
input[type=range]{width:100%;height:44px}
.kv{color:var(--mute);font-size:15px;margin:6px 0}.kv b{color:var(--ink)}
#msg{font-weight:600}#bar{height:8px;border-radius:4px;background:var(--line);overflow:hidden;margin-top:8px}
#bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .3s}
.pad{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;max-width:300px;margin:8px auto}
.steps button{flex:1}.steps button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
img{max-width:100%;border:1px solid var(--line);border-radius:8px;background:#fff;margin-top:8px}
#stop{position:fixed;left:16px;right:16px;bottom:max(16px,env(safe-area-inset-bottom));min-height:64px;font-size:20px;font-weight:700;background:var(--stop);border-color:var(--stop);color:#fff;z-index:9}
</style></head><body>
<header><h1>Plotter Remote</h1><span id="mode" class="badge"></span></header>
<section><h2>Status</h2><div id="msg">…</div><div id="bar"><i></i></div>
<div class="kv">Position from zero: <b id="pos">not set</b></div></section>

<section><h2>1 · Pen height</h2>
<div class="kv">Now: <b id="penz"></b> · Up value: <b id="upz"></b> · Down value: <b id="downz"></b></div>
<input type="range" id="z" min="0" max="9" step="0.5" value="0">
<div class="row"><button id="penup">Pen up</button><button id="pendown">Pen down</button></div>
<div class="row" style="margin-top:10px"><button id="saveup">Save this as UP</button><button id="savedown">Save this as DOWN</button></div>
<div class="kv">Slide to move the pen, then save the height that's right.</div>
<div class="row" style="align-items:center;margin-top:12px"><label for="settle" style="flex:2">Pen settle time (s)</label>
<input id="settle" type="number" min="0" max="5" step="0.1" inputmode="decimal" style="flex:1;min-height:48px;font:inherit;text-align:center;border-radius:11px;border:1px solid var(--line);padding:0 8px;background:var(--card);color:var(--ink)">
<button id="savesettle" style="flex:0 0 90px">Save</button></div>
<button id="testline" style="width:100%;margin-top:10px">Draw 10 mm test line</button>
<div class="kv">Pause after each pen move, so the pen has landed before it moves. If marks come out as dots, raise it. The test line goes 10 mm along +X from where the pen is.</div></section>

<section><h2>2 · Position &amp; zero</h2>
<div class="row steps" id="steps"><button data-s="1">1 mm</button><button data-s="5">5 mm</button><button data-s="10" class="on">10 mm</button><button data-s="50">50 mm</button></div>
<div class="pad">
<span></span><button data-j="0,1">▲ Y+</button><span></span>
<button data-j="-1,0">◀ X−</button><span></span><button data-j="1,0">X+ ▶</button>
<span></span><button data-j="0,-1">▼ Y−</button><span></span></div>
<div class="row"><button id="gozero">Go to zero</button><button id="zero" class="primary">Set zero here</button></div>
<div class="kv">Pen must be up to move. Put the pen at the paper's lower-left corner, then set zero.</div></section>

<section><h2>Start delay</h2>
<div class="row" style="align-items:center"><label for="delay" style="flex:2">Wait before plotting (seconds)</label><input id="delay" type="number" min="0" max="600" step="1" inputmode="numeric" style="flex:1;min-height:48px;font:inherit;border-radius:11px;border:1px solid var(--line);padding:0 12px;background:var(--card);color:var(--ink)"></div>
<div class="kv">Applies to registration marks and plotting. STOP cancels the countdown.</div></section>

<section><h2>3 · Registration marks</h2>
<div class="kv">Paper <b id="paper"></b> <span id="papersrc"></span></div>
<div class="row" style="align-items:center"><label for="tickmm" style="flex:2">Tick size (mm)</label>
<button id="tickdn" style="flex:0 0 56px">−</button>
<input id="tickmm" type="number" min="0.5" max="50" step="0.5" inputmode="decimal" style="flex:1;min-height:48px;font:inherit;text-align:center;border-radius:11px;border:1px solid var(--line);padding:0 8px;background:var(--card);color:var(--ink)">
<button id="tickup" style="flex:0 0 56px">+</button></div>
<button id="reg" style="width:100%;margin-top:10px">Draw registration marks</button></section>

<section><h2>4 · Plot</h2>
<div class="kv">File: <b id="file">none</b></div>
<div id="rotbox" hidden>
<div class="kv">Rotate (clockwise)</div>
<div class="row steps" id="rots"><button data-r="0">0°</button><button data-r="90">90°</button><button data-r="180">180°</button><button data-r="270">270°</button></div>
<div class="kv" id="rotinfo"></div></div>
<div id="layerbox" hidden>
<div class="row" style="align-items:center"><span id="swatch" style="flex:0 0 28px;height:28px;border-radius:50%;border:1px solid var(--line)"></span>
<select id="layer" style="flex:1;min-height:48px;font:inherit;border-radius:11px;border:1px solid var(--line);padding:0 10px;background:var(--card);color:var(--ink)"></select></div>
<div class="kv" id="layerinfo"></div></div><img id="prev" alt="" hidden>
<button id="plot" class="primary" style="width:100%;margin-top:10px">Plot</button></section>

<button id="stop">STOP</button>
<script>
const T=new URLSearchParams(location.search).get('token');
let step=10,S={},delaySet=false,tickSet=false,settleSet=false,layerSig='',previewKey='';
const $=id=>document.getElementById(id);
async function api(path,body){
  const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'X-Token':T,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  const j=await r.json(); if(!r.ok){$('msg').textContent=j.error||'Error';throw new Error(j.error)} return j}
function render(s){S=s;
  $('mode').textContent=s.test_mode?'TEST MODE':'LIVE';$('mode').className='badge '+(s.test_mode?'test':'live');
  $('msg').textContent=(s.busy?'⏳ '+s.busy+' — ':'')+s.message;
  const [d,t]=s.progress;$('bar').firstElementChild.style.width=(s.busy&&t?100*d/t:0)+'%';
  $('pos').textContent=s.pos?s.pos[0].toFixed(1)+', '+s.pos[1].toFixed(1)+' mm (estimate)':'not set';
  $('penz').textContent='Z'+s.pen_z;$('upz').textContent='Z'+s.pen_up_z;$('downz').textContent='Z'+s.pen_down_z;
  $('paper').textContent=s.paper_size;$('papersrc').textContent=s.paper_source==='file'?'(from the loaded file)':'(from profile; file has no page size)';if(!tickSet){$('tickmm').value=s.tick_mm;tickSet=true}
  if(!settleSet){$('settle').value=s.pen_settle_s;settleSet=true}
  $('file').textContent=s.file||'none';$('prev').hidden=!s.has_preview;
  $('rotbox').hidden=!s.has_preview;
  for(const b of document.querySelectorAll('#rots button'))b.classList.toggle('on',+b.dataset.r===s.rotation);
  $('rotinfo').textContent=s.rotation?'Drawing and registration marks are turned '+s.rotation+'° to match the paper.':'';
  const pk=s.has_preview?s.file+'|'+s.layer+'|'+s.rotation:'';
  if(pk&&pk!==previewKey){previewKey=pk;$('prev').src='/api/file.svg?token='+T+'&layer='+encodeURIComponent(s.layer)+'&rot='+s.rotation+'&k='+Date.now()}
  for(const b of document.querySelectorAll('button:not(#stop)'))b.disabled=!!s.busy;
  renderLayers(s);$('layer').disabled=!!s.busy;
  $('plot').disabled=!!s.busy||!s.file;
  if(!delaySet){$('delay').value=s.start_delay_s;delaySet=true}
  if(!document.activeElement||document.activeElement.id!=='z')$('z').value=s.pen_z}
function renderLayers(s){
  const L=s.layers||[];$('layerbox').hidden=L.length===0;
  const sig=JSON.stringify(L);
  if(sig!==layerSig){layerSig=sig;const sel=$('layer');sel.innerHTML='';
    if(L.length>1){const o=document.createElement('option');o.value='all';o.textContent='All layers (one pen)';sel.appendChild(o)}
    L.forEach((l,i)=>{const o=document.createElement('option');o.value=l.id;o.textContent=(i+1)+' · '+l.pen+' · '+l.name+' ('+l.paths+' paths)';sel.appendChild(o)})}
  if(document.activeElement!==$('layer'))$('layer').value=String(s.layer);
  const cur=L.find(l=>String(l.id)===String(s.layer));
  $('swatch').style.background=cur?(cur.hex||'transparent'):'linear-gradient(135deg,#e33,#fc3,#3c6,#36f)';
  $('layerinfo').textContent=cur?'Load the '+cur.pen+' pen before plotting this layer.':(L.length?'Every layer is drawn with the one pen that is loaded.':'');
  $('plot').textContent=cur?'Plot layer: '+cur.name:(L.length?'Plot all layers':'Plot')}
const act=(path,body)=>api(path,body).then(render).catch(()=>{});
$('z').onchange=e=>act('/api/pen',{z:+e.target.value});
$('penup').onclick=()=>act('/api/pen',{z:S.pen_up_z});
$('pendown').onclick=()=>act('/api/pen',{z:S.pen_down_z});
$('saveup').onclick=()=>act('/api/save_height',{which:'up'});
$('savedown').onclick=()=>act('/api/save_height',{which:'down'});
$('savesettle').onclick=()=>act('/api/settle',{seconds:+$('settle').value||0});
$('testline').onclick=()=>confirm('Lower the pen and draw a 10 mm line along +X from here?')&&act('/api/test_line',{mm:10});
$('gozero').onclick=()=>act('/api/goto_zero',{});
$('zero').onclick=()=>confirm('Set the current pen position as zero (X0 Y0)?')&&act('/api/zero',{});
$('reg').onclick=()=>confirm('Draw registration marks at the page corners? Check the pen is loaded and the paper is placed.')&&act('/api/registration',{delay:+$('delay').value||0,tick_mm:+$('tickmm').value||2});
for(const b of document.querySelectorAll('#rots button'))b.onclick=()=>act('/api/rotate',{degrees:+b.dataset.r});
$('layer').onchange=e=>act('/api/layer',{layer:e.target.value==='all'?'all':+e.target.value});
$('plot').onclick=()=>{const cur=(S.layers||[]).find(l=>String(l.id)===String(S.layer));
  const what=cur?'layer "'+cur.name+'" with the '+cur.pen+' pen':(S.layers&&S.layers.length>1?'ALL layers with whatever pen is loaded':S.file);
  if(confirm('Plot '+what+' now? The machine will move.'))act('/api/plot',{delay:+$('delay').value||0})};
const tickStep=d=>{$('tickmm').value=Math.min(50,Math.max(0.5,(+$('tickmm').value||2)+d))};
$('tickdn').onclick=()=>tickStep(-0.5);$('tickup').onclick=()=>tickStep(0.5);
$('stop').onclick=()=>act('/api/stop',{});
for(const b of document.querySelectorAll('[data-j]'))b.onclick=()=>{const [x,y]=b.dataset.j.split(',').map(Number);act('/api/jog',{dx:x*step,dy:y*step})};
for(const b of document.querySelectorAll('#steps button'))b.onclick=()=>{step=+b.dataset.s;for(const o of document.querySelectorAll('#steps button'))o.classList.toggle('on',o===b)};
const poll=()=>api('/api/state').then(render).catch(()=>{});poll();setInterval(poll,1000);
</script></body></html>
"""
