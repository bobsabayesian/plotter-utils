import os
import pty
import re
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


def test_qr_svg_and_qr_outside_notebook(remote):
    svg = pu.qr_svg(remote.url())
    assert svg.startswith("<svg") and "#fff" in svg
    assert remote.qr() is None  # no notebook under pytest: must be a quiet no-op


# ---- layer picker and tick size ---------------------------------------------

def _plotted_xs(remote):
    return [float(l.split("X")[1].split()[0]) for l in remote.conn.sent if l.startswith("G01")]


def test_remote_lists_layers_and_starts_on_the_first(remote):
    state = remote.call("/api/state")[1]
    assert [(l["pen"], l["name"]) for l in state["layers"]] == [("Red", "Red box"), ("Black", "Black"), ("Red", "Red line")]
    assert state["layer"] == state["layers"][0]["id"]  # several layers: not "all"


def test_remote_plots_only_the_selected_layer(remote):
    black = remote.call("/api/state")[1]["layers"][1]["id"]
    assert remote.call("/api/layer", {"layer": black})[1]["layer"] == black
    remote.call("/api/zero", {})
    assert remote.call("/api/plot", {})[0] == 200
    remote.wait_idle()
    xs = _plotted_xs(remote)
    assert xs and max(xs) < 50  # only the black line (x up to 45 mm); the red line sits at 58 mm
    assert not any(abs(x - 31.75) < 0.01 for x in xs)  # and none of the red box's far edge


def test_remote_all_layers_plots_everything_and_a_finished_layer_advances_the_picker(remote):
    layers = remote.call("/api/state")[1]["layers"]
    remote.call("/api/layer", {"layer": "all"})
    remote.call("/api/zero", {})
    remote.call("/api/plot", {})
    remote.wait_idle()
    assert max(_plotted_xs(remote)) > 55  # includes the red line

    remote.conn.sent.clear()
    remote.call("/api/layer", {"layer": layers[0]["id"]})
    remote.call("/api/plot", {})
    remote.wait_idle()
    state = remote.call("/api/state")[1]
    assert state["layer"] == layers[1]["id"] and "Swap to the Black pen" in state["message"]


def test_remote_layer_errors(remote):
    assert remote.call("/api/layer", {"layer": 999})[0] == 409
    remote.call("/api/zero", {})
    remote.call("/api/plot", {"delay": 5})
    assert remote.call("/api/layer", {"layer": "all"})[0] == 409  # busy (counting down)
    remote.call("/api/stop", {})
    remote.wait_idle()


def test_remote_tick_size_is_adjustable(remote):
    assert remote.call("/api/state")[1]["tick_mm"] == 2
    remote.call("/api/zero", {})
    assert remote.call("/api/registration", {"tick_mm": 10})[0] == 200
    remote.wait_idle()
    moves = [l for l in remote.conn.sent if l.startswith("G01")]
    assert "G01 X10.0000 Y10.0000" in moves  # top-left tick now reaches 10 mm in on both axes
    assert remote.call("/api/state")[1]["tick_mm"] == 10
    assert remote.call("/api/registration", {"tick_mm": 0.1})[0] == 409
    assert remote.call("/api/registration", {"tick_mm": 80})[0] == 409
    assert remote.call("/api/state")[1]["tick_mm"] == 10  # a refused value changes nothing


def test_layer_helpers():
    doc = vp.Document(page_size=(100, 100))
    lc = vp.LineCollection()
    lc.append([0j, 10 + 10j])
    doc.add(lc, layer_id=5)
    assert [s.id for s in pu.layer_summaries(doc)] == [5]
    assert pu.extract_layer(doc, 5).layers[5].__len__() == 1
    with pytest.raises(KeyError):
        pu.extract_layer(doc, 6)


def test_remote_go_to_zero(remote):
    assert remote.call("/api/goto_zero", {})[0] == 409  # no zero set yet
    remote.call("/api/zero", {})
    remote.call("/api/jog", {"dx": 30, "dy": 40})
    assert remote.call("/api/state")[1]["pos"] == [30, 40]
    remote.conn.sent.clear()
    state = remote.call("/api/goto_zero", {})[1]
    assert state["pos"] == [0, 0] and remote.conn.sent == ["G90", "G01 X0 Y0 F2000"]
    remote.call("/api/pen", {"z": 6})
    assert remote.call("/api/goto_zero", {})[0] == 409  # pen is down


def test_remote_preview_shows_only_the_selected_layer(remote):
    layers = remote.call("/api/state")[1]["layers"]

    def preview(layer):
        url = f"http://127.0.0.1:{remote.port}/api/file.svg?layer={layer}"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"X-Token": remote.token})) as r:
            assert r.headers["Content-Type"] == "image/svg+xml"
            return r.read().decode()

    whole = preview("all")
    assert all(f'id="layer{l["id"]}"' in whole for l in layers)
    one = preview(layers[1]["id"])
    assert f'id="layer{layers[1]["id"]}"' in one
    assert f'id="layer{layers[0]["id"]}"' not in one and f'id="layer{layers[2]["id"]}"' not in one
    assert 'width="21' in one and 'height="29' in one  # still the full A4 page, so it sits where it will plot
    with pytest.raises(urllib.error.HTTPError) as e:
        preview(999)
    assert e.value.code == 404


def test_pen_settle_dwell_appears_in_gcode(art):
    doc = pu.read_document(art)
    plain = pu.document_to_gcode(doc, pu.MachineProfile())
    settled = pu.document_to_gcode(doc, pu.MachineProfile(pen_settle_s=0.4))
    assert not any(l.startswith("G04") for l in plain)
    i = settled.index("G00 Z6")
    assert settled[i + 1] == "G04 P0.4"  # dwell right after pen down, before drawing
    assert settled[settled.index("G00 Z0", i) + 1] == "G04 P0.4"  # and after pen up


def test_remote_settle_and_test_line(remote):
    assert remote.call("/api/settle", {"seconds": 0.4})[1]["pen_settle_s"] == 0.4
    assert pu.MachineProfile.load(remote.save_path).pen_settle_s == 0.4
    assert remote.call("/api/settle", {"seconds": 9})[0] == 409
    remote.conn.sent.clear()
    assert remote.call("/api/test_line", {})[0] == 200
    assert remote.conn.sent == ["G90", "G00 Z6", "G04 P0.4", "G91", "G01 X10 F2000", "G90", "G00 Z0", "G04 P0.4"]
    remote.call("/api/pen", {"z": 6})
    assert remote.call("/api/test_line", {})[0] == 409  # pen must start up


def test_remote_registration_writes_no_file_and_matches_the_direct_gcode(remote, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(os.listdir(tmp_path))
    remote.call("/api/zero", {})
    remote.conn.sent.clear()
    assert remote.call("/api/registration", {"tick_mm": 5})[0] == 200
    remote.wait_idle()
    assert set(os.listdir(tmp_path)) == before  # nothing written next to the notebook
    direct = pu.document_to_gcode(pu.registration_ticks_document(remote.paper_string(), "5mm"), remote.profile)
    # the in-memory SVG round trip may shift a coordinate by ~0.001 mm, never the structure
    sent = [l for l in remote.conn.sent if l != "G92 X0 Y0"]
    assert [l.split()[0] for l in sent] == [l.split()[0] for l in direct]
    for a, b in zip(sent, direct):
        for wa, wb in zip(a.split()[1:], b.split()[1:]):
            assert wa[0] == wb[0] and float(wa[1:]) == pytest.approx(float(wb[1:]), abs=0.01)


def test_job_registration_goes_to_a_temp_file_by_default(art, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    layers = pu.split_layers(art, output_dir="layers")
    reg = next(s for s in pu.build_layer_job(layers).steps if s.action == "registration")
    assert os.path.dirname(reg.params["filename"]) == __import__("tempfile").gettempdir()
    assert next(s for s in pu.build_layer_job(layers, registration_file="keep.svg").steps
                if s.action == "registration").params["filename"] == "keep.svg"


# ---- rotation ----------------------------------------------------------------

def _doc_with_marker():
    """A 300 x 200 px landscape page with one path from the top-left corner to a marker point."""
    doc = vp.Document(page_size=(300, 200))
    lc = vp.LineCollection()
    lc.append([0j, 50 + 20j])
    doc.add(lc, layer_id=1)
    return doc


@pytest.mark.parametrize("degrees, page, end", [
    (0, (300, 200), (50, 20)),
    (90, (200, 300), (180, 50)),    # top-left corner goes to top-right; (x, y) -> (H - y, x)
    (180, (300, 200), (250, 180)),  # (x, y) -> (W - x, H - y)
    (270, (200, 300), (20, 250)),   # (x, y) -> (y, W - x)
    (-90, (200, 300), (20, 250)),   # negative = counter-clockwise
    (360, (300, 200), (50, 20)),
])
def test_rotate_document_moves_paths_and_page_together(degrees, page, end):
    doc = _doc_with_marker()
    out = pu.rotate_document(doc, degrees)
    assert tuple(out.page_size) == page
    line = out.layers[1][0]
    assert (line[-1].real, line[-1].imag) == pytest.approx(end, abs=1e-6)
    assert doc.page_size == (300, 200) and doc.layers[1][0][-1] == 50 + 20j  # input untouched
    xmin, ymin, xmax, ymax = out.bounds()
    assert xmin >= -1e-6 and ymin >= -1e-6 and xmax <= page[0] + 1e-6 and ymax <= page[1] + 1e-6  # still on the page


def test_rotate_document_rejects_odd_angles():
    with pytest.raises(ValueError):
        pu.rotate_document(_doc_with_marker(), 45)


def test_split_layers_rotate_swaps_the_page(art, tmp_path):
    layers = pu.split_layers(art, output_dir=str(tmp_path / "out"), rotate=90)
    w, h = pu.svg_page_size_mm(layers[0].filename)
    assert (round(w), round(h)) == (297, 210)  # A4 portrait became landscape


def test_remote_rotation_turns_marks_preview_and_plot_and_checks_the_bed(remote):
    state = remote.call("/api/state")[1]
    assert state["rotation"] == 0 and state["paper_size"] == "a4"
    assert remote.call("/api/rotate", {"degrees": 45})[0] == 409
    state = remote.call("/api/rotate", {"degrees": 90})[1]
    assert state["rotation"] == 90 and state["paper_size"] == "a4 (landscape)"

    # the fixture's bed is 210 x 297; the rotated page is 297 wide, so the plot is refused up front
    remote.call("/api/zero", {})
    code, body = remote.call("/api/registration", {})
    assert code == 409 and "outside the bed" in body["error"]
    remote.profile.bed_size_mm = (297, 210)
    remote.conn.sent.clear()
    assert remote.call("/api/registration", {})[0] == 200
    remote.wait_idle()
    xs = [float(l.split("X")[1].split()[0]) for l in remote.conn.sent if " X" in l and "Y" in l]
    ys = [float(l.split("Y")[1].split()[0]) for l in remote.conn.sent if " X" in l and "Y" in l]
    assert max(xs) == pytest.approx(297, abs=0.1) and max(ys) == pytest.approx(210, abs=0.1)

    remote.conn.sent.clear()
    assert remote.call("/api/plot", {})[0] == 200  # red box was top-left; rotated 90 it sits at the right
    remote.wait_idle()
    assert min(_plotted_xs(remote)) > 150

    url = f"http://127.0.0.1:{remote.port}/api/file.svg?layer=all"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"X-Token": remote.token})) as r:
        assert re.search(r'width="29\.7\d*cm"', r.read().decode())  # preview is rotated too (landscape)

    remote.set_file(remote.file)
    assert remote.call("/api/state")[1]["rotation"] == 0  # loading a file resets it


# ---- serial port auto-detect ---------------------------------------------------

def test_resolve_port(monkeypatch, tmp_path, capsys):
    gs = pu.gcode_sender
    real = tmp_path / "ttyACM9"
    real.write_text("")
    monkeypatch.setattr(gs, "find_ports", lambda: [("/dev/ttyACM1", "USB CDC-Serial")])
    assert gs.resolve_port(str(real)) == (str(real), "")                      # exists: used as given
    assert gs.resolve_port("/dev/ttyACM1") == ("/dev/ttyACM1", "")            # a listed device: as given
    dev, note = gs.resolve_port("/dev/ttyACM0")                                # missing, one candidate
    assert dev == "/dev/ttyACM1" and "ttyACM0 not found" in note

    monkeypatch.setattr(gs, "find_ports", lambda: [])
    with pytest.raises(FileNotFoundError, match="no USB serial devices"):
        gs.resolve_port("/dev/ttyACM0")
    monkeypatch.setattr(gs, "find_ports", lambda: [("/dev/ttyACM1", "a"), ("/dev/ttyUSB0", "b")])
    with pytest.raises(FileNotFoundError, match="several candidates"):         # never guesses between two
        gs.resolve_port("/dev/ttyACM0")


def test_connect_uses_the_resolved_port_only_in_live_mode(monkeypatch, capsys):
    gs = pu.gcode_sender
    opened = []
    monkeypatch.setattr(gs, "find_ports", lambda: [("/dev/ttyACM1", "USB CDC-Serial")])
    monkeypatch.setattr(gs, "GrblConnection", lambda port, baud: opened.append((port, baud)) or "conn")
    assert pu.connect(pu.MachineProfile(port="/dev/ttyACM0", test_mode=True)).simulated and not opened
    assert pu.connect(pu.MachineProfile(port="/dev/ttyACM0", test_mode=False)) == "conn"
    assert opened == [("/dev/ttyACM1", 115200)] and "using the only USB serial device" in capsys.readouterr().out


# ---- STOP ---------------------------------------------------------------------

def test_explain_grbl_codes():
    assert "reset while the machine was moving" in pu.explain_grbl("ALARM:3")
    assert "unlocked" in pu.explain_grbl("error:9")
    assert pu.explain_grbl("error:999") == "error:999"  # unknown codes pass through


def test_stop_mid_send_is_a_stop_not_an_alarm_error(monkeypatch):
    """After STOP the controller answers the reset with ALARM:3; that must not surface as a fault."""
    monkeypatch.setattr(pu.gcode_sender.time, "sleep", lambda s: None)

    class FakeSer:
        def __init__(self):
            self.written, self.reads = [], 0
        def write(self, b):
            self.written.append(b)
        def reset_input_buffer(self):
            pass
        def readline(self):
            self.reads += 1
            if self.reads == 2:
                conn.stop()              # the STOP button, pressed while a line is awaiting its 'ok'
                return b"ALARM:3\r\n"   # what a reset mid-move produces
            return b"ok\r\n"

    conn = pu.GrblConnection.__new__(pu.GrblConnection)
    conn.ser, conn._stopped, conn.port = FakeSer(), False, "fake"
    with pytest.raises(pu.GrblStopped):
        conn.send_lines(["G0 X1"] * 5, buffered=False)
    assert conn.ser.written.count(b"\x18") == 1  # reset sent once, not repeated by the error path


def test_a_real_controller_error_still_reports_its_meaning(fake_grbl):
    conn, _ = fake_grbl
    with pytest.raises(pu.GrblError) as e:
        conn.send_lines(["G0 X1", "BAD"])      # the fake answers error:20 to lines containing BAD
    assert "unsupported or invalid" in str(e.value) and not isinstance(e.value, pu.GrblStopped)


def test_remote_stop_during_a_plot_keeps_a_clean_message(art):
    import time as _time

    class SlowSim(pu.SimulatedConnection):
        def send_lines(self, lines, buffered=True, on_progress=None):
            self._stopped = False
            for line in lines:
                if self._stopped:
                    raise pu.GrblStopped("Stopped")
                _time.sleep(0.01)

    r = pu.serve_remote(pu.MachineProfile(), file=art, connection=SlowSim(), host="127.0.0.1", port=0)
    try:
        def call(path, body=None):
            req = urllib.request.Request(f"http://127.0.0.1:{r.port}{path}", data=None if body is None else json.dumps(body).encode(),
                                         headers={"X-Token": r.token})
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        call("/api/zero", {})
        call("/api/plot", {})
        _time.sleep(0.05)
        state = call("/api/stop", {})
        assert state["zero_set"] is False
        for _ in range(100):
            if call("/api/state")["busy"] is None:
                break
            _time.sleep(0.02)
        message = call("/api/state")["message"]
        assert message.startswith("STOPPED") and "ALARM" not in message and "error" not in message.lower()
    finally:
        r.shutdown()
