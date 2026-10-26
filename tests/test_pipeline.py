import os
import pty
import threading

import pytest
import vpype as vp

import plotter_utils as pu


@pytest.fixture
def art(tmp_path):
    """A 3-layer A4 SVG: two red layers and one black."""
    doc = vp.Document(page_size=vp.convert_page_size("a4"))
    specs = [
        ("#ff0000", "Red box", [0j, 100 + 0j, 100 + 100j, 100j, 0j]),
        ("#101010", "Black", [50 + 50j, 150 + 80j]),
        ("#fe0101", "Red line", [200 + 20j, 300 + 20j]),
    ]
    for lid, (color, name, pts) in enumerate(specs, 1):
        lc = vp.LineCollection()
        lc.append([p + 20 + 20j for p in pts])
        lc.set_property(vp.METADATA_FIELD_COLOR, vp.Color(color))
        lc.set_property(vp.METADATA_FIELD_NAME, name)
        doc.add(lc, layer_id=lid, with_metadata=True)
    path = tmp_path / "art.svg"
    with open(path, "w") as f:
        vp.write_svg(f, doc, page_size=doc.page_size)
    return str(path)


@pytest.fixture
def fake_grbl(monkeypatch):
    """A pty standing in for the controller: answers 'ok' to every line."""
    monkeypatch.setattr(pu.gcode_sender.time, "sleep", lambda s: None)
    master, slave = pty.openpty()
    received = []

    def serve():
        buf = b""
        while True:
            buf += os.read(master, 256)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    text = line.strip().decode()
                    received.append(text)
                    os.write(master, b"error:20\r\n" if "BAD" in text else b"ok\r\n")

    threading.Thread(target=serve, daemon=True).start()
    conn = pu.GrblConnection(os.ttyname(slave))
    yield conn, received
    conn.close()


def test_split_layers(art, tmp_path):
    layers = pu.split_layers(art, output_dir=str(tmp_path / "out"))
    assert [l.pen for l in layers] == ["Red", "Black", "Red"]
    assert all(os.path.exists(l.filename) for l in layers)


def test_group_by_pen_merges_layers(art, tmp_path):
    layers = pu.split_layers(art, output_dir=str(tmp_path / "out"), group_by_pen=True)
    assert [l.pen for l in layers] == ["Red", "Black"]
    merged = pu.read_document(layers[0].filename)
    assert sum(len(lc) for lc in merged.layers.values()) == 2


def test_gcode_pen_moves_and_bounds(art):
    doc = pu.read_document(art)
    lines = pu.document_to_gcode(doc, pu.MachineProfile())
    assert lines[:3] == ["G21", "G90", "F2000"]
    assert lines.count("G00 Z6") == lines.count("G00 Z0") - 1  # up at start, then per path
    with pytest.raises(ValueError):
        pu.document_to_gcode(doc, pu.MachineProfile(bed_size_mm=(10, 10)))


def test_profile_round_trip(tmp_path):
    profile = pu.MachineProfile(bed_size_mm=(841, 594), pen_down_z=5)
    profile.save(tmp_path / "p.json")
    assert pu.MachineProfile.load(tmp_path / "p.json") == profile


def test_optimize_reduces_paths():
    doc = vp.Document(page_size=(800, 600))
    lc = vp.LineCollection()
    for a, b in [(0j, 100 + 0j), (500 + 500j, 600 + 500j), (100 + 0j, 100 + 100j), (100 + 100j, 500 + 500j)]:
        lc.append([a, b])
    doc.add(lc, layer_id=1)
    out = pu.optimize_document(doc)
    report = pu.optimization_report(doc, out)
    assert report["before"]["paths"] == 4  # input untouched
    assert 0 < report["after"]["paths"] < report["before"]["paths"]
    assert report["after"]["travel_mm"] <= report["before"]["travel_mm"]
    assert report["after"]["draw_mm"] == pytest.approx(report["before"]["draw_mm"], rel=0.01)


def test_job_json_round_trip_and_dry_run(art, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    layers = pu.split_layers(art, output_dir="layers")
    job = pu.build_layer_job(layers, "a4")
    job.save("jobs/a.json")
    assert pu.Job.load("jobs/a.json").steps == job.steps

    runner = pu.JobRunner(job, pu.JobContext(pu.MachineProfile(bed_size_mm=(210, 297))))
    while not runner.done:
        runner.run_next()
    assert os.path.exists(layers[0].filename.replace(".svg", ".opt.gcode"))


def test_unknown_action_is_reported():
    runner = pu.JobRunner(pu.Job([pu.Step("nope")]), pu.JobContext(pu.MachineProfile()))
    with pytest.raises(KeyError):
        runner.run_next()


@pytest.mark.parametrize("buffered", [True, False])
def test_send_lines(fake_grbl, buffered):
    conn, received = fake_grbl
    progress = []
    conn.send_lines(["G21 ; comment", "(note)", "G0 X1"] + ["G1 X5 Y5"] * 300,
                    buffered=buffered, on_progress=lambda d, t: progress.append((d, t)))
    assert len(received) == 302 and progress[-1] == (302, 302)


def test_error_stops_job(fake_grbl):
    conn, _ = fake_grbl
    with pytest.raises(pu.GrblError, match="line 2"):
        conn.send_lines(["G0 X1", "BAD", "G0 X2"])


def test_test_mode_never_opens_a_port():
    assert pu.MachineProfile().test_mode  # safe by default
    conn = pu.connect(pu.MachineProfile(port="/dev/does-not-exist"))
    assert conn.simulated
    conn.send_lines(["G21", "G0 X1 ; hi"], on_progress=lambda d, t: None)
    assert conn.sent == ["G21", "G0 X1"]


def test_test_mode_rejects_real_connection(art, tmp_path, fake_grbl, monkeypatch):
    monkeypatch.chdir(tmp_path)
    conn, _ = fake_grbl
    layers = pu.split_layers(art, output_dir="layers")
    ctx = pu.JobContext(pu.MachineProfile(test_mode=True), connection=conn)
    runner = pu.JobRunner(pu.Job([pu.Step("plot", {"filename": layers[0].filename})]), ctx)
    with pytest.raises(RuntimeError, match="test_mode"):
        runner.run_next()


def test_job_through_simulated_connection(art, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    layers = pu.split_layers(art, output_dir="layers")
    profile = pu.MachineProfile()
    conn = pu.connect(profile)
    runner = pu.JobRunner(pu.build_layer_job(layers, "a4"), pu.JobContext(profile, connection=conn))
    while not runner.done:
        runner.run_next()
    assert any(l.startswith("G01") for l in conn.sent)


# ---- remote control page --------------------------------------------------

import json
import time
import urllib.error
import urllib.request


@pytest.fixture
def remote(art, tmp_path):
    profile = pu.MachineProfile(bed_size_mm=(210, 297), paper_size="a4")
    r = pu.serve_remote(profile, file=art, host="127.0.0.1", port=0, save_path=str(tmp_path / "m.json"))

    def call(path, body=None, token=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{r.port}{path}",
            data=None if body is None else json.dumps(body).encode(),
            headers={"X-Token": r.token if token is None else token},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait_idle():
        for _ in range(100):
            if call("/api/state")[1]["busy"] is None:
                return
            time.sleep(0.02)
        raise AssertionError("job never finished")

    r.call, r.wait_idle = call, wait_idle
    yield r
    r.shutdown()


def test_remote_requires_token(remote):
    assert remote.call("/api/state", token="wrong")[0] == 403
    assert remote.call("/api/zero", {}, token="wrong")[0] == 403


def test_remote_page_and_state(remote):
    with urllib.request.urlopen(f"http://127.0.0.1:{remote.port}/?token={remote.token}") as r:
        assert b"Plotter Remote" in r.read()
    state = remote.call("/api/state")[1]
    assert state["test_mode"] and not state["zero_set"] and state["has_preview"]


def test_remote_calibrate_and_save(remote):
    assert remote.call("/api/pen", {"z": 4.5})[1]["pen_z"] == 4.5
    assert remote.call("/api/save_height", {"which": "down"})[1]["pen_down_z"] == 4.5
    assert pu.MachineProfile.load(remote.save_path).pen_down_z == 4.5
    assert remote.call("/api/pen", {"z": 42})[0] == 409  # out of range


def test_remote_jog_needs_pen_up_and_respects_bed(remote):
    remote.call("/api/pen", {"z": 6})
    assert remote.call("/api/jog", {"dx": 5, "dy": 0})[0] == 409  # pen is down
    remote.call("/api/pen", {"z": 0})
    assert remote.call("/api/jog", {"dx": 500, "dy": 0})[0] == 409  # over the jog limit
    remote.call("/api/zero", {})
    assert remote.call("/api/jog", {"dx": -5, "dy": 0})[0] == 409  # would leave the bed
    assert remote.call("/api/jog", {"dx": 10, "dy": 20})[1]["pos"] == [10, 20]


def test_remote_plot_requires_zero_then_sends(remote):
    assert remote.call("/api/plot", {})[0] == 409  # zero not set
    remote.call("/api/zero", {})
    assert remote.call("/api/plot", {})[0] == 200
    remote.wait_idle()
    assert any(l.startswith("G01") for l in remote.conn.sent)
    assert remote.call("/api/registration", {})[0] == 200
    remote.wait_idle()
    assert remote.call("/api/state")[1]["message"].startswith("Done")


def test_remote_stop_loses_position_and_laser_files_refused(remote, tmp_path):
    remote.call("/api/zero", {})
    assert remote.call("/api/stop", {})[1]["zero_set"] is False
    laser = tmp_path / "burn.gcode"
    laser.write_text("G21\nM4 S500\nG1 X5 Y5\nM5\n")
    remote.set_file(str(laser))
    remote.call("/api/zero", {})
    code, body = remote.call("/api/plot", {})
    assert code == 409 and "M3/M4" in body["error"]


def test_remote_bounds_error_is_a_refusal_not_a_crash(remote):
    remote.profile.bed_size_mm = (50, 50)
    remote.call("/api/zero", {})
    code, body = remote.call("/api/registration", {})
    assert code == 409 and "outside the bed" in body["error"]


def test_wait_with_cancel():
    import threading
    ev = threading.Event()
    ticks = []
    assert pu.job.wait_with_cancel(0.25, ev, ticks.append) and ticks
    threading.Timer(0.1, ev.set).start()
    assert not pu.job.wait_with_cancel(5, ev)


def test_job_plot_waits_then_sends(art, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    layers = pu.split_layers(art, output_dir="layers")
    profile = pu.MachineProfile(start_delay_s=0.4)
    conn = pu.connect(profile)
    ctx = pu.JobContext(profile, connection=conn)
    step = pu.Step("plot", {"filename": layers[0].filename})
    t0 = time.monotonic()
    pu.job.ACTIONS["plot"](step, ctx)
    assert time.monotonic() - t0 >= 0.4 and conn.sent


def test_job_abort_during_delay_sends_nothing(art, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    layers = pu.split_layers(art, output_dir="layers")
    profile = pu.MachineProfile(start_delay_s=30)
    conn = pu.connect(profile)
    runner = pu.JobRunner(pu.Job([pu.Step("plot", {"filename": layers[0].filename})]),
                          pu.JobContext(profile, connection=conn))
    threading.Timer(0.2, runner.abort).start()
    with pytest.raises(RuntimeError, match="Cancelled"):
        runner.run_next()
    assert conn.sent == [] or conn.sent == ["G92 X0 Y0"]  # abort() stops the connection; no gcode was sent


def test_remote_delay_then_plot_and_cancel(remote):
    remote.call("/api/zero", {})
    assert remote.call("/api/plot", {"delay": 0.5})[0] == 200
    time.sleep(0.15)
    state = remote.call("/api/state")[1]
    assert state["busy"] and "starts in" in state["message"]
    assert not any(l.startswith("G01") for l in remote.conn.sent)  # still counting down
    remote.wait_idle()
    assert any(l.startswith("G01") for l in remote.conn.sent)

    remote.conn.sent.clear()
    assert remote.call("/api/plot", {"delay": 30})[0] == 200
    time.sleep(0.15)
    assert remote.call("/api/stop", {})[1]["zero_set"] is True  # nothing moved, so zero is kept
    remote.wait_idle()
    assert not any(l.startswith("G01") for l in remote.conn.sent)
    assert remote.call("/api/plot", {"delay": 999})[0] == 409


def test_serve_remote_twice_on_same_port_replaces_the_first(art):
    profile = pu.MachineProfile()
    first = pu.serve_remote(profile, host="127.0.0.1", port=0)
    port = first.port
    second = pu.serve_remote(profile, host="127.0.0.1", port=port)  # as if the cell were re-run
    try:
        assert second.port == port and second.token != first.token
    finally:
        second.shutdown()


def test_serve_remote_port_taken_by_another_program_gives_a_clear_error():
    import socket
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    try:
        with pytest.raises(OSError, match="not available"):
            pu.serve_remote(pu.MachineProfile(), host="127.0.0.1", port=blocker.getsockname()[1])
    finally:
        blocker.close()


def test_svg_page_size_is_read_from_the_file(art, tmp_path):
    w, h = pu.svg_page_size_mm(art)
    assert (round(w), round(h)) == (210, 297)
    assert pu.page_size_label(w, h) == "a4"
    assert pu.page_size_label(297, 210) == "a4 (landscape)"
    assert pu.page_size_label(500, 700) == "500.0x700.0mm"
    viewbox_only = tmp_path / "v.svg"
    viewbox_only.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 192"/>')
    assert pu.svg_page_size_mm(str(viewbox_only)) == pytest.approx((25.4, 50.8))
    none = tmp_path / "n.svg"
    none.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100%"/>')
    assert pu.svg_page_size_mm(str(none)) is None


def test_job_paper_size_comes_from_the_file(art, tmp_path):
    layers = pu.split_layers(art, output_dir=str(tmp_path / "out"))
    def reg_size(job):
        return next(s for s in job.steps if s.action == "registration").params["size"]

    assert reg_size(pu.build_layer_job(layers)) == "210.00x297.00mm"
    assert reg_size(pu.build_layer_job(layers, "a3")) == "a3"  # explicit still wins


def test_remote_registration_uses_the_loaded_files_paper(remote):
    state = remote.call("/api/state")[1]
    assert state["paper_size"] == "a4" and state["paper_source"] == "file"
    remote.profile.paper_size = "a1"  # profile says A1, file says A4: file must win
    remote.call("/api/zero", {})
    assert remote.call("/api/registration", {})[0] == 200  # would exceed the A4 bed if A1 were used
    remote.wait_idle()
    xs = [float(l.split("X")[1].split()[0]) for l in remote.conn.sent if l.startswith("G0") and " X" in l and "Y" in l]
    assert max(xs) == pytest.approx(210, abs=0.1)


def test_remote_logs_who_connected(remote, capsys):
    remote.call("/api/state")
    remote.call("/api/zero", {})
    remote.seen()
    out = capsys.readouterr().out
    assert "127.0.0.1" in out and "/api/zero" in out and "/api/state" not in out
    remote.seen(quiet_polls=False)
    assert "/api/state" in capsys.readouterr().out
