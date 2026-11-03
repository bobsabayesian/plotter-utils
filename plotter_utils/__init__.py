"""
Pen plotter utilities. Import once:

    import plotter_utils as pu
    layers = pu.split_layers("art.svg")
    job = pu.build_layer_job(layers)
    pu.job_stepper(job, pu.JobContext(pu.MachineProfile()))

Everything public from the submodules is available as pu.<name>, loaded on first use.
Loading lazily keeps `import plotter_utils` cheap: the G-code agent that runs on a small
board next to the plotter (python -m plotter_utils.agent) only needs pyserial, and never
pays for vpype, numpy or IPython unless something asks for them.
"""
import importlib

# Light modules first, so asking for a light name never loads the heavy ones.
_SUBMODULES = ["gcode_sender", "config", "agent", "agent_client", "gcode", "layers", "optimize",
               "drawing", "compose", "job", "notebook", "remote"]


def _find(name):
    for sub in _SUBMODULES:
        module = importlib.import_module(f".{sub}", __name__)
        if name in getattr(module, "__all__", ()):
            return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    if name in _SUBMODULES:
        return importlib.import_module(f".{name}", __name__)
    value = _find(name)
    globals()[name] = value  # cache, so the lookup runs once per name
    return value


def __dir__():
    names = set(globals()) | set(_SUBMODULES)
    for sub in _SUBMODULES:
        names.update(getattr(importlib.import_module(f".{sub}", __name__), "__all__", ()))
    return sorted(names)
