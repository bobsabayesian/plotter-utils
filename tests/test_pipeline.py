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
