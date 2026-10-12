import vpype

__all__ = ["document_to_gcode", "estimate_plot"]


def document_to_gcode(document, profile, layer_ids=None):
    """
    Converts a vpype.Document into a list of G-code lines for the machine described
    by `profile`: millimeters, absolute, pen lifted with a Z move. Raises ValueError
    if a point falls outside profile.bed_size_mm (when set).
    """
    px_per_mm = vpype.convert_length("1mm")
    height_mm = (document.page_size[1] / px_per_mm) if document.page_size else 0
    ids = list(document.layers) if layer_ids is None else layer_ids

    def pen(z):
        lines = [f"G00 Z{z:g}"]
        if profile.pen_settle_s:
            lines.append(f"G04 P{profile.pen_settle_s:g}")
        return lines

    def point(z):
        x, y = z.real / px_per_mm, z.imag / px_per_mm
        if profile.invert_y:
            y = height_mm - y
        tol = 1e-3  # mm; page-edge points land a hair outside after float conversion
        if profile.bed_size_mm and not (
            -tol <= x <= profile.bed_size_mm[0] + tol and -tol <= y <= profile.bed_size_mm[1] + tol
        ):
            raise ValueError(f"Point ({x:.1f}, {y:.1f}) mm is outside the bed {profile.bed_size_mm}")
        return x, y

    out = ["G21", "G90", f"F{profile.draw_feed:g}"] + pen(profile.pen_up_z)
    for lid in ids:
        for line in document.layers[lid]:
            x, y = point(line[0])
            out.append(f"G00 X{x:.4f} Y{y:.4f}")
            out += pen(profile.pen_down_z)
            for z in line[1:]:
                x, y = point(z)
                out.append(f"G01 X{x:.4f} Y{y:.4f}")
            out += pen(profile.pen_up_z)
    out.append("G00 X0 Y0")
    return out


def estimate_plot(document, profile):
    """
    Rough plot time from path lengths and the draw feed rate. Returns
    {"draw_mm", "travel_mm", "seconds"}. Ignores acceleration and pen-lift time,
    so treat it as a lower bound.
    """
    px_per_mm = vpype.convert_length("1mm")
    draw = document.length() / px_per_mm
    travel = document.pen_up_length()
    if isinstance(travel, tuple):  # some vpype versions return (total, mean)
        travel = travel[0]
    travel /= px_per_mm
    return {
        "draw_mm": draw,
        "travel_mm": travel,
        "seconds": (draw + travel) / (profile.draw_feed / 60),
    }
