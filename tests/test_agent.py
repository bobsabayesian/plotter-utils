"""
The G-code agent and RemoteConnection, end to end, against a fake GRBL board on a pty:
HTTP client -> agent -> serial -> fake controller. No hardware needed.
"""
import json
import os
import pty
import stat
import threading
import time
import types
import urllib.request

import pytest
import vpype

import plotter_utils as pu

TOKEN = "t0k"


class FakeGrbl:
    """Answers like GRBL: 'ok' per line, error:20 for lines containing BAD, a status report for '?',
    and on a reset (0x18) a banner plus ALARM:3. `hold` makes it go silent, like a long move."""

    def __init__(self):
        self.master, self.slave = pty.openpty()
        self.lines, self.realtime = [], []
        self.hold = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def device(self):
        return os.ttyname(self.slave)

    def _write(self, text):
        os.write(self.master, text.encode())

    def _serve(self):
        buf = b""
        while True:
            try:
                data = os.read(self.master, 256)
            except OSError:
                return
            for byte in data:
                if byte in (0x21, 0x18, 0x3F):  # '!', reset, '?'
                    self.realtime.append(byte)
                    if byte == 0x3F:
                        self._write("<Idle|MPos:0.000,0.000,0.000|FS:0,0>\r\n")
                    elif byte == 0x18:
                        self.hold.clear()
                        self._write("\r\nGrbl 1.1h ['$' for help]\r\nALARM:3\r\n")
                elif byte == 0x0A:
                    line, buf = buf.decode().strip(), b""
                    if not line:
                        continue
                    self.lines.append(line)
                    if self.hold.is_set():
                        continue
                    if "BAD" in line:
                        self._write("error:20\r\n")
                    elif line == "$X":
                        self._write("[MSG:Caution: Unlocked]\r\nok\r\n")
                    else:
                        self._write("ok\r\n")
                else:
                    buf += bytes([byte])


@pytest.fixture
def grbl(monkeypatch):
    # Shorten the wake-up and reset pauses (2 s and 0.5 s) for gcode_sender only. They must not vanish: the
    # controller's reply to a reset arrives during the pause and is flushed after it.
    real_sleep = time.sleep
    monkeypatch.setattr(pu.gcode_sender, "time", types.SimpleNamespace(sleep=lambda s: real_sleep(min(s, 0.1))))
    return FakeGrbl()


@pytest.fixture
def agent(grbl):
    a = pu.serve_agent(port=grbl.device, host="127.0.0.1", listen_port=0, token=TOKEN, watchdog_s=0,
                       serial_timeout=0.05)
    a.grbl = grbl
    a.url = f"http://127.0.0.1:{a.listen_port}"
    yield a
    a.shutdown()


@pytest.fixture
def conn(agent):
    return pu.RemoteConnection(agent.url, TOKEN, poll_s=0.02)


def in_thread(fn):
    """Runs fn on a thread; .result() waits and returns (value, exception)."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()

    def result(timeout=5):
        t.join(timeout)
        assert not t.is_alive(), "call did not finish"
        return box.get("value"), box.get("error")
    return result


def wait_for(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---- connecting and authentication ------------------------------------------------------

def test_wrong_token_is_rejected_with_a_helpful_message(agent):
    with pytest.raises(pu.GrblError, match="rejected the token"):
        pu.RemoteConnection(agent.url, "wrong")


def test_unreachable_agent_is_a_clear_error():
    with pytest.raises(pu.GrblError, match="Cannot reach the plotter agent"):
        pu.RemoteConnection("http://127.0.0.1:9", TOKEN, timeout=0.5)


def test_connect_builds_a_remote_connection_from_the_profile(agent, monkeypatch):
    profile = pu.MachineProfile(test_mode=False, agent_url=agent.url, agent_token=TOKEN, pen_up_z=1)
    c = pu.connect(profile)
    assert isinstance(c, pu.RemoteConnection) and c.abort_lines == ["$X", "G00 Z1"]
    monkeypatch.setenv("PLOTTER_AGENT_TOKEN", TOKEN)  # the token can come from the environment instead
    assert isinstance(pu.connect(pu.MachineProfile(test_mode=False, agent_url=agent.url)), pu.RemoteConnection)
    assert pu.connect(pu.MachineProfile(test_mode=True, agent_url=agent.url)).simulated  # test mode wins


# ---- the GrblConnection interface ----------------------------------------------------------

def test_status_and_single_commands(conn, agent):
    assert conn.status().startswith("<Idle")
    conn.set_zero()
    conn.pen_up()
    conn.pen_down(6)
    assert agent.grbl.lines[-3:] == ["G92 X0 Y0", "G00 Z0", "G00 Z6"]


def test_unlock_and_controller_errors(conn):
    assert conn.unlock() == ["[MSG:Caution: Unlocked]"]
    with pytest.raises(pu.GrblError, match="unsupported or invalid"):
        conn.command("BAD")


@pytest.mark.parametrize("buffered", [True, False])
def test_a_job_streams_and_reports_progress(conn, agent, buffered):
    progress = []
    lines = ["G21 ; comment", "(note)", "G0 X1"] + ["G1 X5 Y5"] * 300
    conn.send_lines(lines, buffered=buffered, on_progress=lambda d, t: progress.append((d, t)))
    assert agent.grbl.lines.count("G1 X5 Y5") == 300 and agent.grbl.lines[:2] == ["G21", "G0 X1"]
    assert progress[-1] == (302, 302) and progress == sorted(progress)


def test_a_controller_error_fails_the_job_with_its_meaning(conn, agent):
    with pytest.raises(pu.GrblError, match="unsupported or invalid.*line 2") as e:
        conn.send_lines(["G0 X1", "BAD", "G0 X2"])
    assert not isinstance(e.value, pu.GrblStopped)
    assert agent.job["state"] == "error"


# ---- busy, stop, and the dead-man switch -----------------------------------------------------

def test_one_job_at_a_time_and_stop_ends_it_cleanly(conn, agent):
    agent.grbl.hold.set()  # the controller goes quiet, like a long move
    running = in_thread(lambda: conn.send_lines(["G1 X1"] * 5, buffered=False))
    assert wait_for(lambda: agent.busy)

    with pytest.raises(pu.GrblError, match="job is running"):
        conn.command("G92 X0 Y0")
    other = pu.RemoteConnection(agent.url, TOKEN)
    with pytest.raises(pu.GrblError, match="already running"):
        other.send_lines(["G0 X0"])

    conn.stop()
    _, error = running()
    assert isinstance(error, pu.GrblStopped)           # a stop, not a fault, even though ALARM:3 came back
    assert 0x21 in agent.grbl.realtime and agent.grbl.realtime.count(0x18) == 1  # feed hold, one reset
    assert agent.job["state"] == "stopped" and not agent.busy
    conn.set_zero()                                    # and the agent is usable again


def test_watchdog_stops_the_job_and_lifts_the_pen_when_contact_is_lost(grbl):
    a = pu.serve_agent(port=grbl.device, host="127.0.0.1", listen_port=0, token=TOKEN, watchdog_s=0.4,
                       serial_timeout=0.05)
    try:
        url = f"http://127.0.0.1:{a.listen_port}"
        grbl.hold.set()
        body = json.dumps({"lines": ["G1 X1"] * 5, "buffered": False, "abort_lines": ["$X", "G00 Z0"]}).encode()
        request = urllib.request.Request(url + "/job", data=body, method="POST", headers={"X-Token": TOKEN})
        urllib.request.urlopen(request).read()  # starts the job, then we go silent: no more requests
        assert wait_for(lambda: grbl.lines[-2:] == ["$X", "G00 Z0"], timeout=6)  # stopped, unlocked, pen up
        assert 0x18 in grbl.realtime
        assert a.job["state"] == "stopped" and "Lost contact" in a.job["message"]
    finally:
        a.shutdown()


def test_polling_keeps_the_watchdog_quiet(grbl):
    a = pu.serve_agent(port=grbl.device, host="127.0.0.1", listen_port=0, token=TOKEN, watchdog_s=0.5,
                       serial_timeout=0.05)
    try:
        c = pu.RemoteConnection(f"http://127.0.0.1:{a.listen_port}", TOKEN, poll_s=0.05)
        grbl.hold.set()
        running = in_thread(lambda: c.send_lines(["G1 X1"] * 3, buffered=False))
        time.sleep(1.2)  # well past the watchdog, but the client keeps polling
        assert a.busy and 0x18 not in grbl.realtime
        grbl.hold.clear()
        c.stop()
        running()
    finally:
        a.shutdown()


def test_shutting_down_the_agent_stops_a_running_job(grbl):
    a = pu.serve_agent(port=grbl.device, host="127.0.0.1", listen_port=0, token=TOKEN, watchdog_s=0,
                       serial_timeout=0.05)
    grbl.hold.set()
    body = json.dumps({"lines": ["G1 X1"] * 3, "buffered": False}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{a.listen_port}/job", data=body, method="POST", headers={"X-Token": TOKEN})).read()
    assert wait_for(lambda: a.busy)
    a.shutdown()  # e.g. systemctl stop: the laser must not be left on
    assert 0x18 in grbl.realtime


# ---- the whole stack: tablet page -> RemoteConnection -> agent -> controller ----------------------

def test_the_tablet_page_drives_a_plotter_through_the_agent(agent, tmp_path):
    mm = vpype.convert_length("1mm")
    doc = vpype.Document(page_size=(100 * mm, 60 * mm))
    lc = vpype.LineCollection()
    lc.append([complex(10 * mm, 10 * mm), complex(50 * mm, 10 * mm)])
    doc.add(lc, layer_id=1)
    svg = pu.write_svg(doc, str(tmp_path / "card.svg"))

    profile = pu.MachineProfile(test_mode=False, agent_url=agent.url, agent_token=TOKEN)
    remote = pu.serve_remote(profile, file=svg, connection=pu.connect(profile), host="127.0.0.1", port=0, show_qr=False)
    try:
        def call(path, body=None):
            req = urllib.request.Request(f"http://127.0.0.1:{remote.port}{path}", headers={"X-Token": remote.token},
                                         data=None if body is None else json.dumps(body).encode())
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())

        call("/api/zero", {})
        call("/api/plot", {})
        assert wait_for(lambda: call("/api/state")["busy"] is None)
        assert call("/api/state")["message"].startswith("Done")
        assert "G92 X0 Y0" in agent.grbl.lines
        assert "G01 X50.0000 Y10.0000" in agent.grbl.lines  # the stroke arrived at the controller
    finally:
        remote.shutdown()


# ---- agent odds and ends -------------------------------------------------------------------

def test_status_reports_no_plotter_when_the_port_is_missing():
    a = pu.serve_agent(port="/dev/does-not-exist", host="127.0.0.1", listen_port=0, token=TOKEN, watchdog_s=0)
    try:
        c = pu.RemoteConnection(f"http://127.0.0.1:{a.listen_port}", TOKEN)  # the agent itself is reachable
        assert c.status() is None
        with pytest.raises(pu.GrblError, match="No connection to the plotter"):
            c.command("G21")
    finally:
        a.shutdown()


def test_token_file_is_created_private_and_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("PLOTTER_AGENT_TOKEN", raising=False)
    path = str(tmp_path / "token")
    token, where = pu.agent._load_token(None, path)
    assert "just created" in where and len(token) >= 16
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert pu.agent._load_token(None, path)[0] == token          # reused
    assert pu.agent._load_token("given", path)[0] == "given"     # an explicit token wins
