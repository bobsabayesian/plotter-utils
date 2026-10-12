import threading

from .job import Frontend, JobRunner

__all__ = ["NotebookFrontend", "job_stepper"]


class NotebookFrontend(Frontend):
    """
    Renders runner messages and SVG previews in a Jupyter Output widget.
    """
    def __init__(self, out):
        self.out = out

    def message(self, text):
        from IPython.display import HTML, display
        with self.out:
            display(HTML(f"<h3>{text}</h3>"))

    def preview(self, svg_path):
        from .drawing import preview_svg
        with self.out:
            preview_svg(svg_path)


def job_stepper(job, ctx):
    """
    Shows Next / Abort buttons that walk a Job step by step. Steps run on a worker
    thread, so Abort still works while a plot is in progress. ctx.frontend is
    replaced with a NotebookFrontend.
    """
    from ipywidgets import Button, HBox, Output, VBox
    from IPython.display import display

    out = Output()
    ctx.frontend = NotebookFrontend(out)
    runner = JobRunner(job, ctx)
    btn_next = Button(button_style="info")
    btn_abort = Button(description="Abort Job", button_style="danger")

    def refresh():
        step = runner.next_step
        if runner.aborted:
            btn_next.description, btn_next.disabled = "Aborted", True
        elif step is None:
            btn_next.description, btn_next.button_style, btn_next.disabled = "Job Complete", "success", True
            btn_abort.disabled = True
        else:
            approve = step.action == "plot" and runner.index > 0 and job.steps[runner.index - 1].action == "preview_approval"
            btn_next.description = "Approve & Plot" if approve else f"Run Step {runner.index + 1}/{len(job.steps)}"
            btn_next.button_style = "success" if approve else "info"
            btn_next.disabled = False

    def work():
        try:
            runner.run_next()
        except Exception as e:
            ctx.frontend.message(f"Error: {e}")
        refresh()

    def on_next(_):
        btn_next.disabled = True
        threading.Thread(target=work, daemon=True).start()

    def on_abort(_):
        runner.abort()
        ctx.frontend.message("Job aborted. Fix your SVG and restart.")
        refresh()

    btn_next.on_click(on_next)
    btn_abort.on_click(on_abort)
    refresh()
    display(VBox([HBox([btn_next, btn_abort]), out]))
    return runner
