import vpype

__all__ = [
    "PAPER_SIZE_GROUPS", "page_size_mm", "paper_size_table", "format_paper_sizes",
    "registration_ticks_document", "write_svg", "write_registration_ticks",
    "preview_svg", "generate_registration_ticks", "print_paper_sizes",
    "svg_page_size_mm", "page_size_label", "page_size_string", "registration_ticks_svg",
]

# ==============================================================================
# CORE FUNCTIONS
# Pure: take paths / vpype objects, return values, never print or display.
# Safe to reuse from a GUI.
# ==============================================================================

# Reference sizes shown by the paper size guide: (group, size string, description).
# Standard names are built into vpype; "WIDTHxHEIGHT[unit]" strings are custom sizes.
PAPER_SIZE_GROUPS = [
    ("Standard sizes (built into vpype, case-insensitive)", [
        ("a1", "Standard A1 Blueprint"),
        ("a2", "A2 Art Print"),
        ("a3", "Medium Sketch"),
        ("a4", "A4 Document"),
        ("letter", "Standard Document"),
        ("legal", "Legal Document"),
        ("tabloid", "Tabloid/Ledger"),
        ("executive", "Smaller Notebook"),
    ]),
    ("Postcards", [
        ("4x6in", "Standard US Postcard"),
        ("5x7in", "Large US Postcard"),
        ("a6", "ISO Postcard - built-in name"),
    ]),
    ("Envelopes", [
        ("4.125x9.5in", "US #10 Business"),
        ("4.375x5.75in", "US A2 Invitation"),
        ("4.75x6.5in", "US A6 / Cards"),
        ("5.25x7.25in", "US A7 / Fits 5x7"),
        ("110x220mm", "ISO DL Pocket"),
        ("114x162mm", "ISO C6 Pocket"),
    ]),
]


def page_size_mm(paper_size):
    """
    Returns (width, height) in mm for a standard name ("a1") or a custom
    "WIDTHxHEIGHT[unit]" string ("24x36in"). Case-insensitive.
    """
    w, h = vpype.convert_page_size(paper_size.lower())
    mm = vpype.convert_length("1mm")
    return w / mm, h / mm


def svg_page_size_mm(path):
    """
    Returns the page (width, height) in mm that an SVG declares, from its root
    width/height (any CSS unit) or, failing that, its viewBox (user units = px).
    Returns None if it declares neither. Reads only the root tag, so it is fast on big files.
    """
    import xml.etree.ElementTree as ET
    for _, root in ET.iterparse(path, events=("start",)):
        break  # the first start event is the <svg> root
    mm = vpype.convert_length("1mm")

    def length(value):
        if not value or value.strip().endswith("%"):
            return None
        try:
            return vpype.convert_length(value.strip())
        except ValueError:
            return None

    w, h = length(root.get("width")), length(root.get("height"))
    if w is None or h is None:
        try:
            _, _, vw, vh = (float(v) for v in root.get("viewBox", "").replace(",", " ").split())
            w, h = vw, vh
        except ValueError:
            return None
    return w / mm, h / mm


def page_size_label(width_mm, height_mm):
    """
    A human-readable name for a size: the standard name if it matches one (either
    orientation, within 1 mm), else e.g. "500.0x700.0mm". For display only; to build
    geometry use page_size_string(), which keeps the exact orientation.
    """
    for name in ("a0", "a1", "a2", "a3", "a4", "a5", "a6", "letter", "legal", "tabloid", "executive"):
        sw, sh = page_size_mm(name)
        if (abs(sw - width_mm) < 1 and abs(sh - height_mm) < 1) or (abs(sw - height_mm) < 1 and abs(sh - width_mm) < 1):
            return name if abs(sw - width_mm) < 1 else f"{name} (landscape)"
    return f"{width_mm:.1f}x{height_mm:.1f}mm"


def page_size_string(width_mm, height_mm):
    """
    An exact vpype page-size string, e.g. "594.00x841.00mm".
    """
    return f"{width_mm:.2f}x{height_mm:.2f}mm"


def paper_size_table():
    """
    Returns the paper size guide as data: a list of
    {"group", "size", "description", "width_mm", "height_mm"} dicts.
    """
    rows = []
    for group, entries in PAPER_SIZE_GROUPS:
        for size, description in entries:
            w, h = page_size_mm(size)
            rows.append({"group": group, "size": size, "description": description,
                         "width_mm": w, "height_mm": h})
    return rows


def format_paper_sizes():
    """
    Renders paper_size_table() as a human-readable text guide.
    """
    rule = "=" * 80
    lines = [rule, "PLOTTER PAPER & CARD SIZES".center(80), rule]
    group = None
    for r in paper_size_table():
        if r["group"] != group:
            group = r["group"]
            lines += ["", group.upper(), "-" * 80]
        lines.append(
            f'"{r["size"]}"'.ljust(16)
            + f'{r["width_mm"]:6.1f} x {r["height_mm"]:6.1f} mm | '
            + f'{r["width_mm"] / 25.4:5.2f} x {r["height_mm"] / 25.4:5.2f} in   ({r["description"]})'
        )
    lines += ["", rule,
              'Custom sizes use "WIDTHxHEIGHT[unit]" with no spaces; units: in, cm, mm, px, pc, pt.',
              "To draw corner ticks on any size above:",
              '>> pu.generate_registration_ticks("a1", "2mm")',
              '>> pu.generate_registration_ticks("4x6in", "0.1in", filename="postcard_ticks.svg")',
              rule]
    return "\n".join(lines)


def registration_ticks_document(paper_size="a1", tick_length="2mm"):
    """
    Builds a vpype.Document with a diagonal registration mark pointing inward
    from each corner of the page. tick_length is the distance travelled inward
    on BOTH the X and Y axes, in any vpype unit ("2mm", "0.25in").
    """
    w, h = vpype.convert_page_size(paper_size.lower())
    t = vpype.convert_length(tick_length)

    lc = vpype.LineCollection()
    lc.append([0 + 0j, t + t * 1j])                  # Top-Left
    lc.append([w + 0j, (w - t) + t * 1j])            # Top-Right
    lc.append([w + h * 1j, (w - t) + (h - t) * 1j])  # Bottom-Right
    lc.append([0 + h * 1j, t + (h - t) * 1j])        # Bottom-Left

    doc = vpype.Document(page_size=(w, h))
    doc.add(lc, layer_id=1)
    return doc


def registration_ticks_svg(paper_size="a1", tick_length="2mm"):
    """
    The registration ticks as SVG text, held in memory (nothing is written to disk).
    Feed it to read_document(io.StringIO(text)) to plot it like any other SVG.
    """
    import io
    doc = registration_ticks_document(paper_size, tick_length)
    buf = io.StringIO()
    vpype.write_svg(buf, doc, page_size=doc.page_size)
    return buf.getvalue()


def write_svg(document, filename):
    """
    Writes a vpype.Document to an SVG file at its page size. Returns filename.
    """
    with open(filename, "w") as f:
        vpype.write_svg(f, document, page_size=document.page_size)
    return filename


def write_registration_ticks(paper_size="a1", tick_length="2mm", filename="registration_ticks.svg"):
    """
    Writes corner registration ticks to an SVG file. Returns filename.
    """
    return write_svg(registration_ticks_document(paper_size, tick_length), filename)


# ==============================================================================
# NOTEBOOK WRAPPERS
# Thin layers that print / display. Not for use from a GUI.
# ==============================================================================

def preview_svg(filename):
    """
    Renders an SVG file directly inside the Jupyter Notebook.
    """
    from IPython.display import SVG, display
    display(SVG(filename))


def generate_registration_ticks(paper_size="a1", tick_length="2mm", filename="registration_ticks.svg"):
    """
    Notebook wrapper for write_registration_ticks that reports what it saved.
    """
    write_registration_ticks(paper_size, tick_length, filename)
    print(f"Saved '{filename}' at size {paper_size} with {tick_length} diagonal corner ticks.")


def print_paper_sizes():
    print(format_paper_sizes())
