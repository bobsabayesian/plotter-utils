import json
import os
from dataclasses import asdict, dataclass, field

import vpype

from .drawing import write_registration_ticks
from .gcode import document_to_gcode, estimate_plot
from .layers import read_document
from .optimize import optimization_report, optimize_document

__all__ = [
    "Step", "Job", "build_layer_job", "register_action", "ACTIONS",
    "Frontend", "PrintFrontend", "JobContext", "JobRunner",
]


# ==============================================================================
# PLAN: pure data, serializable to JSON. A GUI can build/edit/save these.
# ==============================================================================

@dataclass
class Step:
    action: str
    params: dict = field(default_factory=dict)

    def to_dict(self):
        return {"action": self.action, **self.params}

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        return cls(d.pop("action"), d)


@dataclass
class Job:
    steps: list = field(default_factory=list)

    def save(self, filename):
        os.makedirs(os.path.dirname(filename) or ".", exist_ok=True)
        with open(filename, "w", encoding="utf-8") as f:
            json.dump([s.to_dict() for s in self.steps], f, indent=2)

    @classmethod
    def load(cls, filename):
        with open(filename, encoding="utf-8") as f:
            return cls([Step.from_dict(d) for d in json.load(f)])


def build_layer_job(layers, paper_size="a1", tick_length="2mm", registration_file="registration.svg", optimize=True):
    """
    Builds the standard per-layer job from split_layers() output: for each layer,
    draw registration ticks, prompt for paper and pen, preview, approve, plot.
    With optimize=True an optimize step runs first and the preview/plot use the
    optimized copy (<layer>.opt.svg).
    """
    steps = []
    for layer in layers:
        plot_file = layer.filename
        if optimize:
            plot_file = layer.filename.replace(".svg", ".opt.svg")
            steps.append(Step("optimize", {"filename": layer.filename, "output": plot_file}))
        steps += [
            Step("registration", {"size": paper_size, "margin": tick_length, "filename": registration_file}),
            Step("user_prompt", {"message": f"Align paper to registration marks and load the {layer.pen} pen."}),
            Step("plot", {"filename": registration_file}),
            Step("preview_approval", {"filename": plot_file, "message": f"Approve the {layer.name} layer"}),
            Step("plot", {"filename": plot_file}),
        ]
    return Job(steps)


# ==============================================================================
# RUNNER: executes steps via a registry. Never prints or draws widgets itself;
# it talks to a Frontend, so the notebook and a future GUI share it.
# ==============================================================================

class Frontend:
    """
    What the runner needs from a user interface. Subclass for a notebook or GUI.
    """
    def message(self, text):
        pass

    def preview(self, svg_path):
        pass


class PrintFrontend(Frontend):
    def message(self, text):
        print(text)


@dataclass
class JobContext:
    """
    Shared state for a run. connection comes from connect(profile); leave it None for a
    file-only dry run, where plot steps write <name>.gcode next to the SVG instead of sending.
    With profile.test_mode on, only a simulated connection is accepted.
    """
    profile: object
    frontend: Frontend = field(default_factory=PrintFrontend)
    connection: object = None
    on_progress: object = None  # callable(done, total) during a plot


ACTIONS = {}


def register_action(name):
    """
    Decorator that adds a step handler: fn(step, ctx). Register your own to add
    new step types to jobs.
    """
    def wrap(fn):
        ACTIONS[name] = fn
        return fn
    return wrap


@register_action("registration")
def _registration(step, ctx):
    p = step.params
    ctx.frontend.message(f"Generating registration marks ({p['size']})...")
    write_registration_ticks(p["size"], p["margin"], filename=p["filename"])
    ctx.frontend.preview(p["filename"])


@register_action("user_prompt")
def _user_prompt(step, ctx):
    ctx.frontend.message(step.params["message"])


@register_action("preview_approval")
def _preview_approval(step, ctx):
    ctx.frontend.message(step.params["message"])
    ctx.frontend.preview(step.params["filename"])


@register_action("optimize")
def _optimize(step, ctx):
    from .drawing import write_svg
    p = step.params
    before = read_document(p["filename"])
    after = optimize_document(before, **p.get("options", {}))
    write_svg(after, p["output"])
    r = optimization_report(before, after)
    ctx.frontend.message(
        f"Optimized {p['filename']}: {r['before']['paths']} -> {r['after']['paths']} paths, "
        f"pen-up travel {r['before']['travel_mm'] / 1000:.2f} -> {r['after']['travel_mm'] / 1000:.2f} m"
    )


@register_action("plot")
def _plot(step, ctx):
    filename = step.params["filename"]
    doc = read_document(filename)
    lines = document_to_gcode(doc, ctx.profile)
    est = estimate_plot(doc, ctx.profile)
    ctx.frontend.message(
        f"{filename}: {est['draw_mm'] / 1000:.2f} m drawn, {est['travel_mm'] / 1000:.2f} m pen-up, "
        f"about {est['seconds'] / 60:.1f} min"
    )
    if ctx.profile.test_mode and not getattr(ctx.connection, "simulated", ctx.connection is None):
        raise RuntimeError("profile.test_mode is on but a real connection was supplied; "
                           "use pu.connect(profile) or set test_mode=False")
    if ctx.connection is None:
        out = os.path.splitext(filename)[0] + ".gcode"
        with open(out, "w") as f:
            f.write("\n".join(lines) + "\n")
        ctx.frontend.message(f"Dry run (no plotter connected): wrote {out}")
    else:
        ctx.connection.send_lines(lines, on_progress=ctx.on_progress)
        ctx.frontend.message("Plot complete.")


class JobRunner:
    """
    Steps through a Job one step at a time (run_next), so a UI can pause for the
    human between steps. abort() stops the plotter if one is connected.
    """
    def __init__(self, job, ctx):
        self.job = job
        self.ctx = ctx
        self.index = 0
        self.aborted = False

    @property
    def done(self):
        return self.aborted or self.index >= len(self.job.steps)

    @property
    def next_step(self):
        return None if self.done else self.job.steps[self.index]

    def run_next(self):
        step = self.next_step
        if step is None:
            return None
        if step.action not in ACTIONS:
            raise KeyError(f"Unknown action {step.action!r}; registered: {sorted(ACTIONS)}")
        ACTIONS[step.action](step, self.ctx)
        self.index += 1
        return step

    def abort(self):
        self.aborted = True
        if self.ctx.connection is not None:
            self.ctx.connection.stop()
