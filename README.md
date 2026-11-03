# plotter-utils

A Python library for driving a pen plotter (and its laser attachment) from SVG artwork, built to be
experimented with in JupyterLab on a headless Raspberry Pi 5 and later reused as the engine of a
full GUI application.

**Target hardware:** UUNA TEK iDraw H, A1 size (GRBL-compatible Drawcore board, USB serial).

```
SVG artwork
   │  split_layers()          one SVG per pen (vpype, colors -> your physical pens)
   ▼
layer SVGs
   │  optimize_document()     merge, sort, reloop, simplify  (less pen-up travel)
   ▼
optimized SVGs
   │  document_to_gcode()     mm, absolute, pen lift via Z moves, bounds-checked
   ▼
G-code lines
   │  GrblConnection          USB serial, streaming, stop / pen / zero helpers
   ▼
plotter
```

A **Job** is a list of steps (draw registration marks, prompt the human, preview, optimize, plot…)
stored as plain JSON. A **JobRunner** executes it through a **Frontend** interface, so the same engine
drives the notebook stepper today and a GUI later.

---

## Contents

1. [Status and honesty box](#status-and-honesty-box)
2. [Install](#install)
3. [Quick start](#quick-start)
4. [First connection checklist](#first-connection-checklist)
5. [Concepts](#concepts)
6. [Module reference](#module-reference)
7. [MachineProfile reference](#machineprofile-reference)
8. [Job file format](#job-file-format)
9. [Extending: custom steps, backends, frontends](#extending)
10. [Raw G-code and the laser](#raw-g-code-and-the-laser)
11. [Remote control from a tablet](#remote-control-from-a-tablet)
12. [Building a GUI on this](#building-a-gui-on-this)
13. [Testing](#testing)
14. [Troubleshooting](#troubleshooting)
15. [Design rules](#design-rules)
16. [Roadmap](#roadmap)
17. [Repository layout](#repository-layout)

---

## Status and honesty box

Everything here was developed **before the plotter arrived**. It is tested against a simulated GRBL
serial port (a pty that answers `ok`), and the SVG/G-code stages are tested on generated artwork.
**Nothing has been run on real iDraw hardware or on the Pi yet.**

| Item | Status |
|---|---|
| SVG splitting, G-code generation, optimization, job JSON, runner | Tested (see `tests/`) |
| Serial streaming against a simulated controller | Tested |
| Notebook widget stepper | Constructed only; buttons not exercised |
| Real iDraw firmware is GRBL | Reported by third-party listings, **unconfirmed** |
| Pen lift = `G00 Z<0-9>`, 0 up / 6 down | From the iDraw **H A3** reference project; **unverified on A1** |
| Y direction (`invert_y`) | **Unverified**; test with a small asymmetric shape |
| Bed size / travel limits | **Unknown**; bounds checking is off until `bed_size_mm` is set |
| `?` status query and buffered streaming on this firmware | **Untested** (`buffered=False` is the safe fallback) |
| G-code agent on a Pi Zero + `RemoteConnection` | Tested against a fake board (including as a separate process); **not yet on a real Zero** |
| Laser use | Raw G-code streaming only; no laser-aware tooling yet |
| Python 3.11 on the Pi | Not yet tried (developed on 3.14 with vpype 1.9) |

Treat the first real session as commissioning, not production. The
[checklist](#first-connection-checklist) is written for that.

---

## Install

On the Pi (or anywhere), use a virtual environment. Prefer **Python 3.11 or 3.12**: current vpype
releases do not support the newest Python versions, and pip will silently pick a very old vpype if
it has to.

```bash
git clone https://github.com/bobsabayesian/plotter-utils.git
cd plotter-utils
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[full]"    # editable; "full" adds vpype, IPython, ipywidgets, segno (everything for the main machine)
pip install jupyterlab      # if not already installed
```

Serial access on Linux needs your user in the `dialout` group (log out and back in afterwards):

```bash
sudo usermod -aG dialout $USER
```

(Plain `pip install -e .` installs only `pyserial`. That is deliberate: it is all the small G-code agent
on a Pi Zero needs. See [Split deployment](#split-deployment-a-pi-zero-next-to-the-plotter).)

The plotter usually appears as `/dev/ttyACM0` (native USB) or `/dev/ttyUSB0` (USB-serial chip).
`pu.find_ports()` lists candidates.

Run the tests:

```bash
pip install -e ".[test]"   # includes the full set of dependencies
pytest
```

---

## Quick start

In a notebook (see [examples/workflow.ipynb](examples/workflow.ipynb)):

```python
%load_ext autoreload
%autoreload 2
import plotter_utils as pu

profile = pu.MachineProfile()                       # defaults; see MachineProfile reference
layers  = pu.split_layers("art.svg", group_by_pen=True)
job     = pu.build_layer_job(layers, optimize=True)   # paper size comes from the SVG
job.save("jobfiles/art.steps.json")

pu.job_stepper(job, pu.JobContext(profile))         # DRY RUN: writes .gcode files, moves nothing
```

Going live (after the checklist below): set `profile.test_mode = False` (or save it in
`machine.json`) and use `connect()`, which is the only place a connection gets made:

```python
profile.test_mode = False
g = pu.connect(profile)                             # real GrblConnection on profile.port
print(g.status())
g.set_zero()                                        # current pen position becomes X0 Y0
pu.job_stepper(job, pu.JobContext(profile, connection=g))
```

### Test mode (until the plotter arrives)

`MachineProfile.test_mode` is **`True` by default**. While it is on:

- `pu.connect(profile)` returns a `SimulatedConnection`: same methods as `GrblConnection`
  (`send_lines`, `set_zero`, `pen_up`, `status`, `stop`, ...), no serial port opened, nothing moves.
  Everything it is asked to send is kept in `conn.sent` so you can inspect it.
- A `plot` step refuses a real `GrblConnection` with a `RuntimeError`, so a stray connection cannot
  move the machine.
- Passing no connection at all still works as a file-only dry run (`.gcode` files are written).

So your notebook code is the same in test and live mode; only `test_mode` changes:

```python
profile = pu.MachineProfile()                       # test_mode=True
g = pu.connect(profile)                             # simulated
pu.job_stepper(job, pu.JobContext(profile, connection=g))
g.sent[:5]                                          # what would have been sent
```

Without any connection, `plot` steps never touch hardware: they write `<file>.gcode` next to the
SVG and report the estimated draw time.

Handy one-offs:

```python
pu.print_paper_sizes()                              # reference table of sizes
pu.generate_registration_ticks("a1", "2mm")         # corner ticks -> registration_ticks.svg
pu.preview_svg("registration_ticks.svg")            # inline in Jupyter
pu.check_gcode("job.gcode")                         # line count, laser use, XY extents
```

---

## First connection checklist

Do these in order, with the pen **removed or raised**, and your hand near the power switch.

1. **Open and identify.**
   ```python
   g = pu.GrblConnection("/dev/ttyACM0")
   print(g.command("$$"))        # GRBL settings dump
   print(g.status())             # e.g. <Idle|MPos:0.000,0.000,0.000|...>
   ```
   If you get GRBL-style output, the assumptions hold. If `status()` returns `None`, this firmware
   may not answer `?`; avoid `status()` and use `buffered=False` everywhere (pass it to
   `send_lines` / `send_file`; `JobContext` currently always uses the buffered default, so this is a
   known gap to close once you see how the real board behaves).
2. **Read the settings.** Note `$32` (laser mode), `$100`/`$101` (steps per mm), `$110`/`$111`
   (max rate), `$130`/`$131` (max travel; this is your real bed size, so put it in
   `bed_size_mm`).
3. **Pen lift.** With the pen clear of the paper:
   ```python
   g.pen_up(0); g.pen_down(6); g.pen_up(0)     # try 0..9, find your own up/down values
   ```
   Put the values in `MachineProfile.pen_up_z` / `pen_down_z`. If the servo needs time to settle,
   set `pen_settle_s`.
4. **Zero.** Move the pen to the lower-left of your paper by hand, then `g.set_zero()`. The iDraw has
   no homing (per the reference project), so **the start position is the origin** and a power
   cycle resets the pen height.
5. **Direction test.** Draw a small asymmetric shape (an "L" or an arrow) from a dry-run `.gcode`
   file with `g.send_file(...)`. If it is mirrored vertically, set `invert_y=True`.
6. **Speed.** Start slow. The reference project allows feeds 50–2500 mm/min; larger machines may
   want lower speeds for quality. Tune `draw_feed`.
7. **Bounds.** Set `bed_size_mm` so out-of-range artwork raises an error instead of stalling a
   motor against the frame. Remember registration ticks sit **exactly on the page corners**.
8. **Emergency stop.** Know that `g.stop()` (or interrupting the Jupyter kernel mid-stream) sends
   feed hold + soft reset. That turns the laser off but **loses position**; re-zero afterwards.

---

## Concepts

**Pure core, thin wrappers.** Core functions take paths or `vpype.Document` objects and return
values. They never `print` or `display`. Notebook-only helpers (`preview_svg`,
`generate_registration_ticks`, `print_paper_sizes`, `job_stepper`) wrap the core. A GUI imports the
core directly.

**vpype `Document`.** The in-memory form of artwork: layers of polylines in CSS pixels (96 per inch)
plus a page size. All geometry passes through it. Convert with `vpype.convert_length("1mm")`.

**Layer = pen.** Each SVG layer's stroke color is matched to the nearest pen in
`MachineProfile.pens` (RGB distance). With `group_by_pen=True`, layers that map to the same pen are
merged into one file so each pen is loaded once and the paths are optimized together.

**Job / Step.** A Job is `[Step(action, params), …]`, plain data, JSON round-trippable.

**Registry + Frontend.** `ACTIONS` maps an action name to a handler `fn(step, ctx)`. Handlers talk to
the user only through `ctx.frontend.message(text)` / `ctx.frontend.preview(svg_path)`.

**Test mode by default.** `MachineProfile.test_mode` is on until you turn it off; see
[Test mode](#test-mode-until-the-plotter-arrives).

---

## Module reference

### `plotter_utils.config`

| Name | Description |
|---|---|
| `MachineProfile` | Dataclass of everything machine-specific. `.save(path)` / `.load(path)` as JSON. |
| `DEFAULT_PENS` | `{name: (r, g, b)}` of the pens in the default profile. |

### `plotter_utils.drawing`

| Function | Description |
|---|---|
| `page_size_mm(size)` | `(w, h)` in mm for `"a1"` or a custom `"24x36in"`. |
| `svg_page_size_mm(path)` | `(w, h)` in mm that an SVG declares (width/height, else viewBox); `None` if neither. |
| `page_size_label(w, h)` / `page_size_string(w, h)` | Display name (`"a1"`, `"a4 (landscape)"`) / exact vpype size string. |
| `paper_size_table()` | Paper-size guide as a list of dicts. |
| `format_paper_sizes()` | The guide as text. |
| `registration_ticks_document(size, tick_length)` | Corner tick marks as a `vpype.Document`. |
| `rotate_document(doc, degrees=90)` | Rotated copy of a Document, clockwise (multiples of 90; negative = counter-clockwise). Swaps the page size for quarter turns and moves the paths so they still fill the page; the input is untouched. |
| `bounding_box_document(doc, which="page")` / `document_to_svg_text(doc)` | A Document holding one closed rectangle for the page edge (`"page"`) or the drawn paths' bounds (`"art"`) / any Document as in-memory SVG text. |
| `registration_ticks_svg(size, tick_length)` | The same marks as SVG text, in memory (the remote page plots this; no file is written). |
| `write_svg(document, filename)` | Save a Document as SVG at its page size. |
| `write_registration_ticks(size, tick_length, filename)` | Ticks straight to a file. |
| `preview_svg(filename)` | *(notebook)* show an SVG inline. |
| `generate_registration_ticks(...)` | *(notebook)* write ticks and print a confirmation. |
| `print_paper_sizes()` | *(notebook)* print the guide. |

Custom page sizes are `"WIDTHxHEIGHT[unit]"` with no spaces; units `in, cm, mm, px, pc, pt`.

### `plotter_utils.layers`

| Name | Description |
|---|---|
| `read_document(svg)` | Multi-layer SVG -> `Document` (curves flattened to 0.1 mm, transforms applied). |
| `match_pen(rgb, pens=None)` | Nearest pen name; `"Unassigned"` for no color. |
| `layer_summaries(doc, pens=None)` / `extract_layer(doc, layer_id)` | Describe a Document's layers (`id`, `name`, `pen`, `hex_color`, `paths`) / get a one-layer copy. The remote page's picker uses these. |
| `split_layers(svg, output_dir="layerfile", pens=None, group_by_pen=False, rotate=0)` | One SVG per layer (or per pen). Returns `LayerInfo(index, name, pen, hex_color, filename)` list. |

### `plotter_utils.agent` and `plotter_utils.agent_client`

| Name | Description |
|---|---|
| `python -m plotter_utils.agent` / `plotter-agent` | The agent process for the machine on the plotter's USB port (needs only `pyserial`). |
| `serve_agent(port, baud, host, listen_port, token, watchdog_s, serial_timeout)` | Starts an `Agent` on a background thread and returns it (`.shutdown()` stops it). |
| `RemoteConnection(url, token, timeout, poll_s, lost_after_s, abort_lines)` | `GrblConnection`'s methods, carried out through an agent. `connect(profile)` builds one when `profile.agent_url` is set. |

### `plotter_utils.compose`

| Name | Description |
|---|---|
| `compose_cards(items, sheet_mm, gap_mm=10, margin_mm=10, rotate=0, pens=None)` | Tiles SVGs on one sheet and merges their layers by pen. `items`: paths, `(path, copies)` pairs, or `{"file", "copies", "rotate"}` dicts. Returns `(Document, [Placement])`; raises `ValueError` naming a card that doesn't fit. |
| `Placement` | `name`, `x_mm`, `y_mm`, `width_mm`, `height_mm` (the card's page), `art_mm` (its drawn paths' box on the sheet). |
| `cards_outline_document(placements, sheet_size_px, which)` | One rectangle per card: `"cards"` (page edge) or `"card_art"` (artwork bounds). |
| `cards_registration_document(placements, sheet_size_px, tick_mm)` | Four corner ticks inside every card. |

### `plotter_utils.optimize`

| Name | Description |
|---|---|
| `optimize_document(doc, merge, sort, reloop, simplify, merge_tolerance, simplify_tolerance)` | Returns a new, optimized Document; the input is untouched. Uses vpype's `linemerge`, `linesort`, `reloop`, `linesimplify`. |
| `optimization_report(before, after)` | Path counts, drawn length, and pen-up travel for both (mm). |

### `plotter_utils.gcode`

| Name | Description |
|---|---|
| `document_to_gcode(doc, profile, layer_ids=None)` | G-code lines: `G21`, `G90`, `F<feed>`; per path: pen up, `G00` to start, pen down, `G01` segments; finishes pen up and `G00 X0 Y0`. Raises `ValueError` outside `bed_size_mm`. |
| `estimate_plot(doc, profile)` | `{"draw_mm", "travel_mm", "seconds"}`. A lower bound: ignores acceleration and pen-lift time. |

Sample output:

```gcode
G21
G90
F2000
G00 Z0
G00 X5.2917 Y5.2917
G00 Z6
G01 X31.7500 Y5.2917
...
G00 Z0
G00 X0 Y0
```

### `plotter_utils.gcode_sender`

| Name | Description |
|---|---|
| `find_ports()` | Likely USB serial devices `(device, description)`. |
| `resolve_port(port)` | `(device, note)`: the port as given if it exists, else the only attached USB serial device (with a note), else a clear error. Never guesses between several. `connect()` uses it. |
| `read_gcode(file)`, `clean_gcode_line(l)` | Load a file, stripping `;` and `( )` comments and blank lines. |
| `check_gcode(file)` | Dry summary: line count, laser/spindle use (`M3`/`M4`), XY extents. |
| `connect(profile)` | The connection factory: `SimulatedConnection` when `profile.test_mode`, else `GrblConnection`. |
| `SimulatedConnection()` | Hardware-free stand-in with the same methods; records lines in `.sent`. |
| `GrblConnection(port, baud=115200)` | Context-manager serial connection (see below). |
| `GrblError` | Raised on `error:N`, `ALARM:N` (with a plain-language meaning added, see `explain_grbl`), or a long silence. |
| `GrblStopped` | A `GrblError` subclass raised when a job is stopped on purpose (`stop()` / the STOP button), so you can tell a stop from a fault. |

`GrblConnection` methods:

| Method | Description |
|---|---|
| `status()` | Raw `?` status report, or `None` if unanswered. |
| `command(line)` | Send one line, return response lines up to `ok`. |
| `send_lines(lines, buffered=True, on_progress=None)` | Stream lines; blocks until all are acknowledged. |
| `send_file(path, buffered=True, on_progress=None)` | Same, from a file. |
| `set_zero()` | `G92 X0 Y0`: current position becomes the origin. |
| `pen_up(z=0)`, `pen_down(z=6)` | `G00 Z<z>`. |
| `unlock()` | `$X`. Only after you know why it alarmed. |
| `stop()` | Feed hold + soft reset; callable from another thread; aborts a running send. |
| `close()` | Close the port (also done by `with`). |

Streaming modes: `buffered=True` uses GRBL character counting (keeps the 127-byte RX buffer full
so motion does not stall between lines). `buffered=False` sends one line and waits for `ok`, which is
slower but is the proven handshake used by the iDraw reference project. If the firmware misbehaves,
switch to unbuffered first.

### `plotter_utils.job`

| Name | Description |
|---|---|
| `Step(action, params)` | One step. `to_dict()` / `from_dict()` flatten params next to `action`. |
| `Job(steps)` | `.save(path)` / `Job.load(path)` JSON. |
| `build_layer_job(layers, paper_size=None, tick_length, registration_file=None, optimize=True)` | The standard per-layer sequence (below). `paper_size=None` takes the page size from the SVG; pass a string to override. Registration marks go to a temporary file unless you pass `registration_file`. |
| `register_action(name)` | Decorator adding a step handler. |
| `ACTIONS` | The registry (`registration`, `user_prompt`, `preview_approval`, `optimize`, `plot`). |
| `Frontend`, `PrintFrontend` | UI interface and a trivial print implementation. |
| `JobContext(profile, frontend, connection, on_progress)` | Shared run state. |
| `JobRunner(job, ctx)` | `run_next()`, `.done`, `.next_step`, `.index`, `abort()`. |

The per-layer sequence from `build_layer_job`:

1. `optimize`: write `<layer>.opt.svg` and report the travel saved *(if `optimize=True`)*
2. `registration`: write corner ticks
3. `user_prompt`: "Align paper to the registration marks and load the <pen> pen."
4. `plot`: draw the registration ticks
5. `preview_approval`: show the layer, wait for you
6. `plot`: draw the layer

### `plotter_utils.remote`

| Name | Description |
|---|---|
| `serve_remote(profile, file=None, connection=None, host="0.0.0.0", port=8000, optimize=True, tick_length="2mm", allow_laser=False, save_path=None)` | Start the tablet page on a background thread; returns a `RemoteControl`. Uses a simulated connection in test mode. |
| `qr_svg(text)` | A QR code as an SVG string. |
| `RemoteControl` | `.url()`, `.qr()`, `.set_batch(items, sheet_mm, ...)`, `.select_layer(id_or_"all")`, `.set_tick(mm)`, `.seen()`, `.set_file(path)`, `.stop()` (emergency), `.shutdown()`; its actions mirror the page buttons. |

### `plotter_utils.notebook`

| Name | Description |
|---|---|
| `job_stepper(job, ctx)` | Next / Abort buttons. Each step runs on a worker thread, so **Abort works during a plot**. Returns the `JobRunner`. |
| `NotebookFrontend(out)` | A `Frontend` that renders into an `ipywidgets.Output`. |

---

## MachineProfile reference

| Field | Default | Meaning |
|---|---|---|
| `name` | `"idraw-h-a1"` | Label. |
| `test_mode` | `True` | No hardware: `connect()` returns a simulator and real connections are refused. Set `False` once the plotter is here. |
| `port` | `"/dev/ttyACM0"` | Serial device. |
| `baud` | `115200` | Serial speed. |
| `agent_url` | `""` | If set (e.g. `"http://plotter-zero.local:8765"`), `connect()` returns a `RemoteConnection` to the G-code agent there instead of opening `port`. |
| `agent_token` | `""` | The agent's token; or set `PLOTTER_AGENT_TOKEN`. Saved in plain text by `profile.save()`. |
| `paper_size` | `"a1"` | Fallback page size, used only when a file declares none (e.g. raw G-code). SVG jobs and the remote page use the file's own page size. |
| `bed_size_mm` | `None` | `(width, height)` for bounds checking; `None` disables it. Set from `$130/$131` or by measuring. |
| `pen_up_z` | `0` | Z value (0–9) with the pen lifted. |
| `pen_down_z` | `6` | Z value with the pen on paper. **Calibrate.** |
| `pen_settle_s` | `0` | Dwell (`G04 P…`) after every pen move. |
| `start_delay_s` | `0` | Countdown (seconds) before any `plot` step, or the remote page's registration/plot, starts sending. Abort / STOP cancels it before anything moves. Per-step override: `{"action": "plot", "filename": ..., "delay": 10}`. |
| `draw_feed` | `2000` | Feed rate, mm/min. |
| `invert_y` | `False` | Flip Y if drawings come out vertically mirrored. |
| `pens` | 7 CMY+ pens | `{name: (r, g, b)}` used by `split_layers` color matching. |

```python
profile = pu.MachineProfile(bed_size_mm=(841, 594), pen_down_z=5)
profile.save("machine.json")
profile = pu.MachineProfile.load("machine.json")
```

---

## Job file format

A JSON array of steps; each step is `{"action": ..., <params>}`. Files written by the earlier
notebook prototype load unchanged.

```json
[
  {"action": "optimize", "filename": "layerfile/00_Red_Box_ff0000.svg",
   "output": "layerfile/00_Red_Box_ff0000.opt.svg"},
  {"action": "registration", "size": "a1", "margin": "2mm", "filename": "registration.svg"},
  {"action": "user_prompt", "message": "Align paper to registration marks and load the Red pen."},
  {"action": "plot", "filename": "registration.svg"},
  {"action": "preview_approval", "filename": "layerfile/00_Red_Box_ff0000.opt.svg",
   "message": "Approve the Box layer"},
  {"action": "plot", "filename": "layerfile/00_Red_Box_ff0000.opt.svg"}
]
```

| Action | Params | Effect |
|---|---|---|
| `registration` | `size`, `margin`, `filename` | Writes corner ticks and previews them. |
| `user_prompt` | `message` | Shows an instruction to the human. |
| `preview_approval` | `filename`, `message` | Shows the SVG for sign-off. |
| `optimize` | `filename`, `output`, optional `options` (kwargs of `optimize_document`) | Writes an optimized copy and reports savings. |
| `plot` | `filename`, optional `delay` (seconds; default `profile.start_delay_s`) | Converts to G-code; sends it if `ctx.connection` is set, otherwise writes `<name>.gcode`. |

---

## Extending

### A new step type

```python
@pu.register_action("pause")
def _pause(step, ctx):
    ctx.frontend.message(f"Pausing: {step.params['reason']}")

job.steps.insert(3, pu.Step("pause", {"reason": "swap the paper"}))
```

Rules for handlers: take `(step, ctx)`, report only through `ctx.frontend`, read hardware state
from `ctx` (profile, connection), and keep parameters JSON-serializable so jobs stay saveable.

### A new frontend (GUI, web, CLI)

Subclass `pu.Frontend` and implement `message(text)` and `preview(svg_path)`; add methods as your UI
needs them (progress, confirmation). Then drive the runner yourself:

```python
runner = pu.JobRunner(job, pu.JobContext(profile, frontend=MyFrontend(), connection=g))
while not runner.done:
    ask_user_to_continue(runner.next_step)     # your UI
    runner.run_next()                          # run on a worker thread for long plots
```

### A new machine or output (another backend)

`document_to_gcode` is the machine-specific piece: it reads a `MachineProfile` and returns lines.
For a different controller, write a sibling function with the same signature (for example a laser
raster/vector function emitting `M4 S…` moves) and branch on a profile field in the `plot` handler.
When a second backend appears, extract a small interface (`plot(document)`, `estimate(document)`,
`stop()`) rather than adding `if` branches.

### Other ideas that fit the shape

- more optimize options as a step (per-pen tolerances, reordering across layers)
- a `pen_change` step that parks the head at a safe spot
- resume-from-line after an abort (record acknowledged line counts in the runner)
- hatch fill / text-to-path steps that produce Documents before `plot`

---

## Raw G-code and the laser

You can send any G-code file directly:

```python
pu.check_gcode("engrave.gcode")        # review first: line count, M3/M4 use, XY extents
with pu.GrblConnection("/dev/ttyACM0") as g:
    g.set_zero()
    g.send_file("engrave.gcode", buffered=False, on_progress=lambda d, t: print(d, "/", t, end="\r"))
```

Laser safety, since there is no laser-aware tooling yet:

- Enable GRBL laser mode (`$32=1`) so power follows motion; without it the head can pause with the
  beam on at every power change. Check with `g.command("$$")`.
- Test with low power / high speed on scrap, wear proper eye protection, and never leave it unattended.
- `stop()` sends feed hold and a soft reset, which cancels `M3/M4` output on stock GRBL. Verify on
  your unit that the laser actually goes dark when you stop it.
- `check_gcode` will tell you whether a file turns the spindle/laser on before you send it.

---

## Remote control from a tablet

`serve_remote()` starts a small web page on the Pi so you can run the plotter from an iPad or phone
standing next to it, while your notebook stays in the other room.

```python
profile = pu.MachineProfile.load("machine.json")        # test_mode still True? the page runs a simulator
remote = pu.serve_remote(profile, file="layerfile/00_Red_Box_ff0000.svg", save_path="machine.json")
# Plotter remote (TEST MODE): http://192.168.4.220:8000/?token=...   <- open this on the tablet
```

The cell returns immediately; the server runs on a background thread.

| Page section | What it does |
|---|---|
| **Pen height** | Slider moves the pen to any Z 0–9; **Pen up / Pen down** go to the stored values; **Save this as UP / DOWN** stores the current height in the profile (and in `save_path` if given). **Pen settle time** is the pause after every pen move (`profile.pen_settle_s`); **Draw 10 mm test line** lowers the pen and draws a short line along +X so you can tune the down height and settle time. If marks come out as dots, the pen is moving before it has landed: raise the settle time. |
| **Position & zero** | Jog X/Y by 1, 5, 10 or 50 mm; **Set zero here** (`G92 X0 Y0`); **Go to zero** returns the raised pen to that point in a straight line. Shows an estimated position from zero. |
| **Outline** | Draws a rectangle for the **paper edge** (the SVG's page, rotated if you rotated it) or the **artwork box** (the smallest box around all the drawn paths), at the same position the plot will use, so you can mark where the paper goes, e.g. with a dry-erase pen on a whiteboard, before placing it. Shows the size in mm and inches. Needs zero set and an SVG; it uses whatever pen is loaded. |
| **Registration marks** | Draws corner ticks at the corners of the **loaded file's page size** (read from the SVG; the page shows which). The **Tick size (mm)** box (with − / +) sets how far in the diagonal runs, 0.5–50 mm. Falls back to `profile.paper_size` for raw G-code files. |
| **Rotate** | For an SVG, turns the drawing 0 / 90 / 180 / 270° **clockwise**, the same as turning the paper. The preview, the plot, the registration marks and the paper-size readout all follow (a landscape page becomes portrait). If the rotated page doesn't fit `bed_size_mm`, plotting is refused before anything moves. Rotation resets when you load another file. |
| **Layer picker** | For an SVG, lists its layers as `1 · Red · Red box (12 paths)` with a color swatch and which pen to load. **Plot** draws only the selected layer; "All layers (one pen)" draws everything with whatever pen is loaded (the confirm dialog says so). With several layers the picker starts on the first, not "All". When a layer finishes it advances to the next and the status says which pen to swap in. |
| **Plot** | Previews the SVG (just the selected layer, at its place on the page, when you pick one) and plots the selected layer (optimized, unless `optimize=False`). A raw `.gcode` file is sent as is. |
| **Unlock machine** | In the Status section: sends `$X` (after a confirmation) to clear the controller's alarm lock, which a STOP mid-move often leaves behind. Not available while a job runs. Position stays unknown, so jog and set zero afterwards. A "locked" (`error:9`) refusal on any move now tells you to press it. |
| **STOP** | Always visible. Feed hold + soft reset; position is lost, so set zero again. |

Safeguards built in:

- **Token.** The URL carries a random token; every request needs it. That is the only access control,
  and traffic is plain HTTP, so use it on a trusted home network only. Anyone with the link can move
  the machine. Restarting `serve_remote` makes a new token.
- **Order of operations.** Plotting and registration are refused until zero is set; moving needs the
  pen up; one job runs at a time (a second tap is rejected); only STOP works during a job.
- **Limits.** Jogs are capped at 100 mm and, once zero is set and `bed_size_mm` is known, are refused
  if they would leave the bed. Z is limited to 0–9.
- **Start delay.** A "Start delay" box on the page (default `profile.start_delay_s`) counts down before registration or a plot starts; STOP cancels the countdown without losing your zero.
- **Confirmations.** Set zero, registration and plot each ask "are you sure?" on the tablet.
- **Laser files.** A raw G-code file containing `M3`/`M4` is refused unless you start the server with
  `allow_laser=True`.
- **Test mode.** With `profile.test_mode` on, the page drives a `SimulatedConnection`: everything
  works except the machine moves. Try the page on the tablet like this first. The page shows a
  TEST MODE / LIVE badge.

`serve_remote` also shows the link as a **QR code** in the notebook; point the tablet's camera at it and tap the banner instead of typing the link. `remote.qr()` shows it again (`by_name=True` for the `.local` link), and `serve_remote(..., show_qr=False)` turns it off.

`remote.seen()` prints the latest requests that reached the server (time, client IP, browser), which is the quickest way to tell whether a tablet is reaching the machine at all.

### Batches of cards (tiling several SVGs)

To plot many cards in one session, tile them on one sheet (your whiteboard or bed) and let the page
do the rest:

```python
remote = pu.serve_remote(profile, save_path="machine.json")
remote.set_batch(
    ["thanks.svg", ("birthday.svg", 4), {"file": "invite.svg", "copies": 2, "rotate": 90}],
    sheet_mm=(841, 594),      # the area to fill; defaults to profile.bed_size_mm
    gap_mm=10, margin_mm=10,  # space between cards / around the edge
)
```

- **Layout.** Cards fill the sheet left to right, then wrap to a new row. A card that doesn't fit is
  named in the error, with how many were placed before it. The preview shows a dashed box around every
  card.
- **Layers merge by pen.** Every card's strokes for a pen become one layer, so the layer picker lists
  *pens*, not cards, and each pen is loaded once for the whole batch. Optimizing then also orders paths
  across cards, which cuts travel.
- **Outline.** The choices become **Each card** (the edge of every card's paper, for marking where each
  piece goes in dry erase), **Card artwork** (a box around each card's drawn paths) and **Sheet edge**.
- **Registration marks.** Ticks go on the corners of every card, not the sheet.
- **Rotation** is set per card (`rotate=` in `set_batch`, or per item); the page's Rotate buttons are
  off in a batch.
- `remote.set_file(...)` goes back to a single file. The composed sheet lives in a temporary folder that
  is removed on `remote.shutdown()`.

Typical session: `set_batch`, set zero at the sheet's top-left, draw **Each card** with the dry-erase
pen, place the paper in each box, optionally draw the registration marks, then plot each pen's layer.

The same composition works without the page:
`sheet, placements = pu.compose_cards(items, (841, 594))`, then `pu.write_svg(sheet, "sheet.svg")` and
the usual `split_layers` / `build_layer_job`. In that route the job's registration marks go on the
sheet's corners, not on each card.

Other calls: `remote.set_file("other.svg")` to change the file, `remote.url()` to reprint the link,
`remote.shutdown()` to stop serving. For live use, `profile.test_mode = False` first.

Limits to know: position is **dead-reckoned** from your jogs (the iDraw has no position feedback
we read yet), so it is an estimate; and the page was exercised by automated tests against the
simulator, **not yet on a real iPad or the real machine**.

---

## Building a GUI on this

The package is shaped so the GUI is a new frontend, not a rewrite:

| GUI feature | Backed by |
|---|---|
| Open an SVG, show layers and pens | `split_layers`, `LayerInfo`, `MachineProfile.pens` |
| Pen mapping editor | `MachineProfile.pens`, `match_pen` |
| Preview / zoom | `Document` to SVG via `write_svg`; or render `Document.layers` directly |
| Optimization panel with before/after | `optimize_document`, `optimization_report` |
| Time and distance readout | `estimate_plot` |
| Job editor (reorder, add steps) | `Job`, `Step`, `ACTIONS` keys as a step palette |
| Run / pause / abort | `JobRunner`, `GrblConnection.stop()`; run steps on a worker thread |
| Settings screen | `MachineProfile` fields |
| Jog / zero / pen test | `GrblConnection.command`, `set_zero`, `pen_up`, `pen_down` |
| Live progress | `on_progress(done, total)` in `JobContext` / `send_lines` |

Things a GUI will need that are not built yet: a pause/resume (feed hold `!` / cycle start `~` are
defined as constants in `gcode_sender`), a position readout parsed from `status()`, and a place
to persist the runner's progress for resuming a half-done job.

---

## Testing

```bash
pytest
```

`tests/test_agent.py` runs HTTP client -> agent -> serial -> a fake GRBL board (stalls, errors, resets with `ALARM:3`): jobs, progress, STOP, the one-job rule, the watchdog, the token, and the tablet page through the whole stack. `tests/test_pipeline.py` covers layer splitting and pen grouping, G-code generation and bounds
checking, profile and job round-trips, optimization (including that the input is not mutated),
a dry-run job from SVG to `.gcode`, and the serial streamer in both modes (plus error handling)
against a pty that stands in for the controller. None of it needs hardware.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `FileNotFoundError ... no USB serial devices detected` | The plotter isn't visible to this machine: check power, use a data-capable USB cable, and make sure Jupyter is on the machine it is plugged into. `ls /dev/ttyACM* /dev/ttyUSB*` and `dmesg \| tail` show what the OS sees. |
| Port number keeps changing (`ttyACM0` -> `ttyACM1`) | It re-enumerates on replug, especially if an old kernel still holds the port. `connect()` finds the only device automatically; for a name that never changes use the `/dev/serial/by-id/...` path from `ls -l /dev/serial/by-id/` as `profile.port`. |
| After STOP, the next move fails with `error:9` / the board seems locked | Resetting mid-move leaves GRBL in an alarm state (`ALARM:3`, "reset while moving"). It refuses G-code until unlocked: press **Unlock machine** on the page (or run `remote.conn.unlock()` / `g.unlock()`, which send `$X`), then jog and set zero again. The STOP button itself reports "STOPPED. Position lost…" and nothing else. |
| `PermissionError: /dev/ttyACM0` | Add yourself to `dialout`, re-login. |
| `import vpype` fails or pip installs vpype 1.9 | Python too new; use 3.11/3.12 in the venv. |
| `GrblError: No response from controller` | Wrong port/baud, board not GRBL, or a very long move; try `buffered=False`. |
| `GrblError: error:N …` | GRBL rejected a line; `N` is the GRBL error code. The message includes the line. |
| `ALARM` on connect | `g.unlock()` (`$X`) once you know why; check limits/e-stop. |
| Drawing is mirrored vertically | `invert_y=True` in the profile. |
| Drawing is offset | You did not `set_zero()` at the paper's lower-left, or paper moved. |
| Pen drags or does not mark | Calibrate `pen_down_z`; add `pen_settle_s`. |
| `Point (…) mm is outside the bed` | Artwork or registration ticks exceed `bed_size_mm`; resize, or check the bed value. |
| Pen unassigned in `split_layers` | The SVG layer has no stroke color; set it in the editor (Inkscape layers) or edit `pens`. |
| Abort button seems frozen | Run `pu.job_stepper` (threaded) rather than calling `run_next()` in a notebook cell. |
| Kernel interrupt during a plot | Triggers `stop()`; position is lost, re-zero before restarting. |

---

## Design rules

1. **Core is pure.** No `print`, `display`, or widgets outside `notebook.py` and the notebook
   wrappers in `drawing.py`.
2. **Plans are data.** Jobs and profiles serialize to JSON so a GUI can edit and store them.
3. **Test mode by default.** Hardware moves only when `test_mode` is off *and* you hand over a real `connection`.
4. **One source of machine truth.** Hardware numbers live in `MachineProfile`, not in notebooks.
5. **Fail loud before the machine moves.** Bounds checks and G-code generation happen before any
   serial write.
6. **Mark unverified assumptions** (this README does) until they are tested on the real machine.

---

## Roadmap

- Commission on real hardware; fill in the verified values above and tighten the checklist
- Persist pen calibration and bed size per machine
- Pause / resume and resume-after-abort
- Laser-aware output (vector and raster) and a laser job type
- Position readout and jog controls
- Backend interface once there is a second machine type
- ~~Split deployment: a thin G-code agent on a Pi Zero~~ built (see [Split deployment](#split-deployment-a-pi-zero-next-to-the-plotter)); still to do: try it on the real Zero and plotter, and a way to restart or update the agent remotely
- Larger paper/tiling workflow for artwork bigger than the bed
- The GUI

---

## Split deployment: a Pi Zero next to the plotter

Run the heavy parts (SVG processing, the tablet page, notebooks) on one machine and put a small
**G-code agent** on a Pi Zero that has the plotter on its USB port.

```
main machine (this repo, vpype, tablet page)  --LAN-->  Pi Zero: plotter-agent  --USB-->  plotter
 SVG -> optimize -> G-code                                owns the serial port, streams each
 RemoteConnection                                         job to GRBL locally
```

The main machine sends a whole job to the agent, which streams it to the controller locally and
reports progress. A Wi-Fi hiccup therefore can't stall a stroke mid-line. `RemoteConnection` has the same
methods as `GrblConnection`, so the pipeline, the job runner and the tablet page need no changes: set
`profile.agent_url` and `pu.connect(profile)` returns it.

> **Status:** built and tested against a fake GRBL board (the agent as a real process, a pyserial-only
> environment, the whole stack through the tablet page). **Not yet run on a real Pi Zero or the plotter.**

### Setting up the Zero

1. **Flash Raspberry Pi OS Lite** with Raspberry Pi Imager; in its settings set a hostname (say
   `plotter-zero`), enable SSH and enter your Wi-Fi. Use a **Zero 2 W** or Zero W: the original Zero has
   no Wi-Fi. The Zero's data port is micro-USB, so use an OTG adapter to the plotter's cable; the
   plotter keeps its own power. Use a decent power supply: brown-outs show up as random alarms.
2. **Install only the agent** (it needs just `pyserial`):
   ```bash
   sudo apt install -y python3-venv git
   git clone https://github.com/bobsabayesian/plotter-utils.git
   python3 -m venv ~/plotter-venv
   ~/plotter-venv/bin/pip install ./plotter-utils        # no [full]: no vpype, numpy or IPython
   sudo usermod -aG dialout $USER                        # then log out and back in
   ls /dev/ttyACM* /dev/ttyUSB*                          # the plotter should be listed
   ```
3. **Run it once by hand:**
   ```bash
   ~/plotter-venv/bin/plotter-agent
   ```
   It prints its address and, the first time, a token (saved to `~/.plotter_agent_token`, readable only
   by you). Options: `--port` (default `auto`: the only USB serial device, else say which),
   `--listen-port 8765`, `--watchdog 20`, `--token` / `$PLOTTER_AGENT_TOKEN`.
4. **Point the main machine at it.** In the notebook:
   ```python
   profile = pu.MachineProfile(test_mode=False,
                               agent_url="http://plotter-zero.local:8765",
                               agent_token="<the token>")   # or export PLOTTER_AGENT_TOKEN
   g = pu.connect(profile)        # a RemoteConnection; fails with a clear message if unreachable
   print(g.status())              # e.g. <Idle|MPos:0.000,0.000,0.000|...>
   ```
   Then everything else works as before: `pu.serve_remote(profile, connection=g, ...)`, the job stepper,
   `g.send_file("job.gcode")`. The [first-connection checklist](#first-connection-checklist) applies
   unchanged. Save the URL (and token) with `profile.save("machine.json")`; the token is stored in
   plain text, so keep that file private.
5. **Run it at boot** with systemd:
   ```bash
   sudo cp plotter-utils/deploy/plotter-agent.service /etc/systemd/system/
   sudo nano /etc/systemd/system/plotter-agent.service   # set User= and the venv path
   sudo systemctl daemon-reload && sudo systemctl enable --now plotter-agent
   journalctl -u plotter-agent -f
   ```
   `systemctl stop` sends SIGTERM; the agent stops a running job (so the laser can't stay on) before it exits.
6. **Optional:** turn off Wi-Fi power saving on the Zero for steadier latency
   (`sudo iw dev wlan0 set power_save off`; add it to a boot script to keep it).

### How it behaves

| Situation | What happens |
|---|---|
| A job is sent | The agent streams it locally; the main machine polls progress (`on_progress` works as usual). |
| Short commands (pen moves, jog, set zero) | Complete inside one request, so they feel instant. |
| STOP | One message: feed hold + reset on the agent. The running `send_lines` raises `GrblStopped`. Works any time, even mid-job. |
| One job at a time | A second job, or a single command while a job runs, is refused with a clear message. |
| Main machine disappears mid-job (**watchdog**) | The main side polls the agent while a job runs. If nothing is heard for `--watchdog` seconds (default 20) the agent stops the job (the reset turns the laser off), then runs the connection's abort lines (`$X`, then a pen-up move) so the pen isn't left on the paper. `--watchdog 0` turns it off. |
| Agent restarted or crashed mid-job | The job is lost. A clean stop (`systemctl stop`) stops the controller first; a hard crash can't, so check the pen and re-zero. |
| Plotter unplugged or replugged | The agent drops the dead port and reopens it on the next request (the port number may change; `auto` finds it). |
| Wrong or missing token | `RemoteConnection` raises a clear error naming `agent_token` / `PLOTTER_AGENT_TOKEN`. |

**Security.** The token is the only protection and traffic is plain HTTP: anyone on your network who has
it can move the machine. Use a trusted network, don't expose the port to the internet, and treat the token
like a key.

**Protocol** (JSON over HTTP, `X-Token` header): `GET /status`; `POST /job {"lines", "buffered",
"abort_lines", "wait_s"}`; `POST /command {"line"}`; `POST /unlock`; `POST /stop`.

**Troubleshooting.**
- `Cannot reach the plotter agent`: check the URL and port, that the agent is running
  (`systemctl status plotter-agent`), and that both machines are on the same network.
- `No connection to the plotter` (HTTP 503): the agent is up but can't open the serial port. Check the
  cable and power, `ls /dev/ttyACM*` on the Zero, and the `dialout` group.
- `status()` returns `None` during a job: expected. The agent's streamer owns the serial port while a
  job runs.
- Two things must not use the plotter at once: don't open the serial port directly on the Zero while the
  agent is running.

---

## Repository layout

```
plotter_utils/
  __init__.py       single import point; re-exports every module's __all__
  config.py         MachineProfile, DEFAULT_PENS
  drawing.py        paper sizes, registration ticks, SVG helpers, notebook wrappers
  layers.py         SVG layer splitting, pen matching, grouping
  compose.py        tile several SVGs on one sheet, merge layers by pen, per-card outlines
  optimize.py       vpype path optimization and report
  gcode.py          Document -> G-code, time estimate
  gcode_sender.py   GRBL serial connection and raw G-code tools
  job.py            Step/Job plan, action registry, Frontend, JobRunner
  agent.py          G-code agent for the machine on the plotter (pyserial only)
  agent_client.py   RemoteConnection: GrblConnection's interface through an agent
  notebook.py       ipywidgets stepper frontend
  remote.py         tablet web UI: calibrate, jog, zero, registration, plot
examples/
  workflow.ipynb    end-to-end example notebook
tests/
  test_pipeline.py  hardware-free tests
deploy/
  plotter-agent.service  systemd unit for the agent
pyproject.toml      packaging (core = pyserial; extras: full, test)
```

`__all__` in each module controls what `import plotter_utils as pu` exposes; to add a module, give
it an `__all__` and add `from .yourmodule import *` to `__init__.py`.
