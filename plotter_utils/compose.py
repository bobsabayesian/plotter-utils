import os
from dataclasses import dataclass

import vpype

from .config import DEFAULT_PENS
from .drawing import corner_tick_lines, rotate_document
from .layers import _describe, read_document

__all__ = ["Placement", "compose_cards", "cards_outline_document", "cards_registration_document"]


@dataclass
class Placement:
    """One card on the sheet, in mm from the sheet's top-left corner."""
    name: str
    x_mm: float
    y_mm: float
    width_mm: float       # the card's page, not just its art
    height_mm: float
    art_mm: tuple = None  # (x0, y0, x1, y1) of the card's drawn paths on the sheet; None if it is empty


def _normalize(items, rotate):
    """Accepts "card.svg", ("card.svg", copies) or {"file":..., "copies":..., "rotate":...}."""
    for item in items:
        if isinstance(item, dict):
            yield str(item["file"]), int(item.get("copies", 1)), item.get("rotate", rotate)
        elif isinstance(item, (tuple, list)):
            yield str(item[0]), int(item[1]), rotate
        else:
            yield str(item), 1, rotate


def compose_cards(items, sheet_mm, gap_mm=10, margin_mm=10, rotate=0, pens=None):
    """
    Lays several SVGs ("cards") out on one sheet, left to right and top to bottom,
    and merges their layers BY PEN, so a single layer holds every card's strokes for
    that pen and you load each pen once.

    items      paths, (path, copies) pairs, or {"file", "copies", "rotate"} dicts
    sheet_mm   (width, height) of the area to fill, e.g. your bed or whiteboard
    gap_mm     space between cards; margin_mm  space around the edge of the sheet
    rotate     turns every card 0/90/180/270 degrees clockwise (per-item "rotate" wins)

    Returns (Document, [Placement]). The Document's page is the sheet, so it can go
    straight through the normal pipeline (optimize, G-code, split_layers, ...).
    Raises ValueError, naming the card, if one does not fit.
    """
    pens = DEFAULT_PENS if pens is None else pens
    mm = vpype.convert_length("1mm")
    sheet_w, sheet_h = sheet_mm
    eps = 0.01  # mm; reading an SVG adds ~0.0001 mm of noise, which must not push an exact fit onto a new row

    cards = []
    for path, copies, rot in _normalize(items, rotate):
        doc = read_document(path)
        if rot:
            doc = rotate_document(doc, rot)
        stem = os.path.splitext(os.path.basename(path))[0]
        for n in range(1, copies + 1):
            cards.append((f"{stem} #{n}" if copies > 1 else stem, doc))
    if not cards:
        raise ValueError("No cards given")

    # Shelf layout: fill a row left to right, wrap when the next card would overflow
    x = y = margin_mm
    row_h = 0.0
    placed = []
    for name, doc in cards:
        w, h = round(doc.page_size[0] / mm, 3), round(doc.page_size[1] / mm, 3)
        if x > margin_mm and x + w > sheet_w - margin_mm + eps:
            x, y, row_h = margin_mm, y + row_h + gap_mm, 0.0
        if x + w > sheet_w - margin_mm + eps or y + h > sheet_h - margin_mm + eps:
            raise ValueError(
                f"{name} ({w:.0f} x {h:.0f} mm) does not fit on the {sheet_w:.0f} x {sheet_h:.0f} mm sheet "
                f"after {len(placed)} card(s); use a bigger sheet, smaller gaps/margins, or fewer copies"
            )
        placed.append((name, doc, x, y, w, h))
        x += w + gap_mm
        row_h = max(row_h, h)

    merged = {}      # pen -> LineCollection, in order of first appearance
    placements = []
    for name, doc, x, y, w, h in placed:
        for lid in sorted(doc.layers):
            layer = doc.layers[lid]
            if layer.is_empty():
                continue
            _, pen, _ = _describe(layer, lid, pens)
            lines = vpype.LineCollection(layer)  # a copy; the source document is shared between copies
            lines.translate(x * mm, y * mm)
            merged.setdefault(pen, vpype.LineCollection()).extend(lines)
        bounds = doc.bounds()
        art = None if bounds is None else (x + bounds[0] / mm, y + bounds[1] / mm, x + bounds[2] / mm, y + bounds[3] / mm)
        r = lambda v: round(float(v), 3)  # drop float noise from unit conversion
        placements.append(Placement(name, r(x), r(y), r(w), r(h), None if art is None else tuple(r(v) for v in art)))

    sheet = vpype.Document(page_size=(sheet_w * mm, sheet_h * mm))
    for layer_id, (pen, lines) in enumerate(merged.items(), 1):
        lines.set_property(vpype.METADATA_FIELD_NAME, pen)
        if pen in pens:
            lines.set_property(vpype.METADATA_FIELD_COLOR, vpype.Color("#%02x%02x%02x" % tuple(pens[pen])))
        sheet.add(lines, layer_id=layer_id, with_metadata=True)
    return sheet, placements


def cards_outline_document(placements, sheet_size_px, which="cards"):
    """
    A Document with one closed rectangle per card: "cards" is each card's page edge (where
    its paper goes), "card_art" each card's drawn-paths bounds. Raises ValueError otherwise.
    """
    if which not in ("cards", "card_art"):
        raise ValueError("which must be 'cards' or 'card_art'")
    mm = vpype.convert_length("1mm")
    lc = vpype.LineCollection()
    for p in placements:
        if which == "cards":
            x0, y0, x1, y1 = p.x_mm, p.y_mm, p.x_mm + p.width_mm, p.y_mm + p.height_mm
        elif p.art_mm is None:
            continue
        else:
            x0, y0, x1, y1 = p.art_mm
        lc.append([complex(x0 * mm, y0 * mm), complex(x1 * mm, y0 * mm), complex(x1 * mm, y1 * mm),
                   complex(x0 * mm, y1 * mm), complex(x0 * mm, y0 * mm)])
    if lc.is_empty():
        raise ValueError("Nothing to outline")
    doc = vpype.Document(page_size=tuple(sheet_size_px))
    doc.add(lc, layer_id=1)
    return doc


def cards_registration_document(placements, sheet_size_px, tick_mm=2):
    """
    A Document with four corner ticks inside each card's page edge, so every card's paper
    gets its own registration marks.
    """
    mm = vpype.convert_length("1mm")
    lc = vpype.LineCollection()
    for p in placements:
        for line in corner_tick_lines(p.x_mm * mm, p.y_mm * mm, (p.x_mm + p.width_mm) * mm,
                                      (p.y_mm + p.height_mm) * mm, tick_mm * mm):
            lc.append(line)
    doc = vpype.Document(page_size=tuple(sheet_size_px))
    doc.add(lc, layer_id=1)
    return doc
