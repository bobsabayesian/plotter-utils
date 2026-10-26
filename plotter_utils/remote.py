import hmac
import json
import os
import re
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .drawing import registration_ticks_document
from .gcode import document_to_gcode, estimate_plot
from .gcode_sender import connect, read_gcode
from .job import wait_with_cancel
from .layers import read_document
from .optimize import optimize_document

__all__ = ["RemoteControl", "serve_remote"]

# Servers started in this process. setdefault keeps the list across %autoreload, so a
# re-run cell can still find and stop the server it started last time.
_ACTIVE = globals().setdefault("_ACTIVE", [])

MAX_JOG_MM = 100
Z_RANGE = (0, 9)


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
        self.tick_length = tick_length
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
        self.cancel = threading.Event()
        self.counting = False  # True during the start delay, before the machine has moved

    # ---- state -------------------------------------------------------------

    def state(self):
        p = self.profile
        return {
            "test_mode": p.test_mode, "busy": self.busy, "progress": self.progress,
            "message": self.message, "pen_z": self.pen_z, "pen_up_z": p.pen_up_z,
            "pen_down_z": p.pen_down_z, "pos": self.pos, "zero_set": self.pos is not None,
            "file": os.path.basename(self.file) if self.file else None,
            "has_preview": bool(self.file and self.file.lower().endswith(".svg")),
            "paper_size": p.paper_size, "tick_length": self.tick_length,
            "z_range": Z_RANGE, "start_delay_s": self.profile.start_delay_s,
        }

    def set_file(self, path):
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self.file = path
        self.message = f"File set: {os.path.basename(path)}"

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

    def _require_ready(self):
        if self.pos is None:
            raise RemoteError("Set zero first (jog the pen to the paper's lower-left corner)")
        if self.pen_z != self.profile.pen_up_z:
            raise RemoteError("Raise the pen first")

    def start_registration(self, delay=None):
        self._require_ready()
        doc = registration_ticks_document(self.profile.paper_size, self.tick_length)
        self._start_job("registration marks", document_to_gcode(doc, self.profile), delay=delay)

    def start_plot(self, delay=None):
        self._require_ready()
        if not self.file:
            raise RemoteError("No file chosen")
        if self.file.lower().endswith(".svg"):
            doc = read_document(self.file)
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
        self._start_job(os.path.basename(self.file), lines, note, delay)

    def _start_job(self, name, lines, note="", delay=None):
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
                self.message = f"Done: {name}"
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
                path = urlparse(self.path).path
                if not self._authorized():
                    return self._send(403, {"error": "bad token"})
                if path == "/":
                    return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                if path == "/api/state":
                    return self._send(200, remote.state())
                if path == "/api/file.svg" and remote.file and remote.file.lower().endswith(".svg"):
                    with open(remote.file, "rb") as f:
                        return self._send(200, f.read(), "image/svg+xml")
                self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._authorized():
                    return self._send(403, {"error": "bad token"})
                path = urlparse(self.path).path
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                routes = {
                    "/api/pen": lambda: remote.move_pen(body["z"]),
                    "/api/save_height": lambda: remote.save_pen_height(body["which"]),
                    "/api/jog": lambda: remote.jog(body["dx"], body["dy"]),
                    "/api/zero": remote.set_zero,
                    "/api/registration": lambda: remote.start_registration(body.get("delay")),
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
                 tick_length="2mm", allow_laser=False, save_path=None):
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
<div class="kv">Slide to move the pen, then save the height that's right.</div></section>

<section><h2>2 · Position &amp; zero</h2>
<div class="row steps" id="steps"><button data-s="1">1 mm</button><button data-s="5">5 mm</button><button data-s="10" class="on">10 mm</button><button data-s="50">50 mm</button></div>
<div class="pad">
<span></span><button data-j="0,1">▲ Y+</button><span></span>
<button data-j="-1,0">◀ X−</button><span></span><button data-j="1,0">X+ ▶</button>
<span></span><button data-j="0,-1">▼ Y−</button><span></span></div>
<button id="zero" class="primary" style="width:100%">Set zero here</button>
<div class="kv">Pen must be up to move. Put the pen at the paper's lower-left corner, then set zero.</div></section>

<section><h2>Start delay</h2>
<div class="row" style="align-items:center"><label for="delay" style="flex:2">Wait before plotting (seconds)</label><input id="delay" type="number" min="0" max="600" step="1" inputmode="numeric" style="flex:1;min-height:48px;font:inherit;border-radius:11px;border:1px solid var(--line);padding:0 12px;background:var(--card);color:var(--ink)"></div>
<div class="kv">Applies to registration marks and plotting. STOP cancels the countdown.</div></section>

<section><h2>3 · Registration marks</h2>
<div class="kv">Paper <b id="paper"></b> · tick <b id="tick"></b></div>
<button id="reg" style="width:100%">Draw registration marks</button></section>

<section><h2>4 · Plot file</h2>
<div class="kv">File: <b id="file">none</b></div><img id="prev" alt="" hidden>
<button id="plot" class="primary" style="width:100%;margin-top:10px">Plot this file</button></section>

<button id="stop">STOP</button>
<script>
const T=new URLSearchParams(location.search).get('token');
let step=10,S={},delaySet=false;
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
  $('paper').textContent=s.paper_size;$('tick').textContent=s.tick_length;
  $('file').textContent=s.file||'none';$('prev').hidden=!s.has_preview;
  if(s.has_preview&&!$('prev').src)$('prev').src='/api/file.svg?token='+T;
  for(const b of document.querySelectorAll('button:not(#stop)'))b.disabled=!!s.busy;
  $('plot').disabled=!!s.busy||!s.file;
  if(!delaySet){$('delay').value=s.start_delay_s;delaySet=true}
  if(!document.activeElement||document.activeElement.id!=='z')$('z').value=s.pen_z}
const act=(path,body)=>api(path,body).then(render).catch(()=>{});
$('z').onchange=e=>act('/api/pen',{z:+e.target.value});
$('penup').onclick=()=>act('/api/pen',{z:S.pen_up_z});
$('pendown').onclick=()=>act('/api/pen',{z:S.pen_down_z});
$('saveup').onclick=()=>act('/api/save_height',{which:'up'});
$('savedown').onclick=()=>act('/api/save_height',{which:'down'});
$('zero').onclick=()=>confirm('Set the current pen position as zero (X0 Y0)?')&&act('/api/zero',{});
$('reg').onclick=()=>confirm('Draw registration marks at the page corners? Check the pen is loaded and the paper is placed.')&&act('/api/registration',{delay:+$('delay').value||0});
$('plot').onclick=()=>confirm('Plot '+S.file+' now? The machine will move.')&&act('/api/plot',{delay:+$('delay').value||0});
$('stop').onclick=()=>act('/api/stop',{});
for(const b of document.querySelectorAll('[data-j]'))b.onclick=()=>{const [x,y]=b.dataset.j.split(',').map(Number);act('/api/jog',{dx:x*step,dy:y*step})};
for(const b of document.querySelectorAll('#steps button'))b.onclick=()=>{step=+b.dataset.s;for(const o of document.querySelectorAll('#steps button'))o.classList.toggle('on',o===b)};
const poll=()=>api('/api/state').then(render).catch(()=>{});poll();setInterval(poll,1000);
</script></body></html>
"""
