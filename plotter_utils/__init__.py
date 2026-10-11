"""
Pen plotter utilities. Import once:

    import plotter_utils as pu
    pu.print_paper_sizes()
    pu.generate_registration_ticks("a1")
    pu.GrblConnection("/dev/ttyUSB0")

Everything public from the submodules is re-exported here.
"""
from .drawing import *
from .gcode_sender import *
