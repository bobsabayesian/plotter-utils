import json
from dataclasses import asdict, dataclass, field

__all__ = ["MachineProfile", "DEFAULT_PENS"]

# Physical pens you own: name -> (R, G, B). Layer colors are matched to the closest.
DEFAULT_PENS = {
    "Cyan": (0, 255, 255),
    "Magenta": (255, 0, 255),
    "Yellow": (255, 255, 0),
    "Black": (0, 0, 0),
    "Red": (255, 0, 0),
    "Blue": (0, 0, 255),
    "Green": (0, 128, 0),
}


@dataclass
class MachineProfile:
    """
    Everything machine-specific, in one place. Defaults follow the iDraw H A3
    reference project (github.com/MOVzeroOne/python_controler_idraw); the pen
    heights, bed size and Y direction are UNVERIFIED on the A1 -- confirm them
    on the real machine.
    """
    name: str = "idraw-h-a1"
    test_mode: bool = True         # no hardware: connect() returns a simulator; set False once the plotter is here
    port: str = "/dev/ttyACM0"
    baud: int = 115200
    paper_size: str = "a1"
    bed_size_mm: tuple = None      # (width, height); None disables bounds checking
    pen_up_z: float = 0            # the iDraw lifts the pen with a Z move, 0-9
    pen_down_z: float = 6
    pen_settle_s: float = 0        # dwell after each pen move, if the pen needs time to land
    start_delay_s: float = 0       # countdown before any plot/registration starts; Abort/STOP cancels it
    draw_feed: float = 2000        # mm/min; the reference project allows 50-2500
    invert_y: bool = False         # flip Y if drawings come out mirrored vertically
    pens: dict = field(default_factory=lambda: dict(DEFAULT_PENS))

    def save(self, filename):
        with open(filename, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, filename):
        with open(filename) as f:
            data = json.load(f)
        if data.get("bed_size_mm"):
            data["bed_size_mm"] = tuple(data["bed_size_mm"])
        data["pens"] = {k: tuple(v) for k, v in data.get("pens", {}).items()}
        return cls(**data)
