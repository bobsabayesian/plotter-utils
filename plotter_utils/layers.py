import os
import re
from dataclasses import dataclass

import vpype

from .config import DEFAULT_PENS

__all__ = ["LayerInfo", "LayerSummary", "match_pen", "split_layers", "read_document", "layer_summaries", "extract_layer"]


@dataclass
class LayerInfo:
    index: int
    name: str
    pen: str
    hex_color: str
    filename: str


@dataclass
class LayerSummary:
    id: int            # vpype layer id (what extract_layer takes)
    name: str
    pen: str
    hex_color: str
    paths: int


def _describe(layer, lid, pens):
    """(name, pen, hex_color) for one vpype layer."""
    color = layer.property(vpype.METADATA_FIELD_COLOR) if layer.property_exists(vpype.METADATA_FIELD_COLOR) else None
    name = layer.property(vpype.METADATA_FIELD_NAME) if layer.property_exists(vpype.METADATA_FIELD_NAME) else f"layer{lid}"
    rgb = _rgb(color)
    return name, match_pen(rgb, pens), "#%02x%02x%02x" % rgb if rgb else ""


def layer_summaries(document, pens=None):
    """
    Describes each non-empty layer of a vpype.Document, in layer-id order, as
    LayerSummary(id, name, pen, hex_color, paths). Used for layer pickers.
    """
    out = []
    for lid in sorted(document.layers):
        layer = document.layers[lid]
        if not layer.is_empty():
            name, pen, hex_color = _describe(layer, lid, pens)
            out.append(LayerSummary(lid, name, pen, hex_color, len(layer)))
    return out


def extract_layer(document, layer_id):
    """
    Returns a new Document holding only one layer of `document` (same page size,
    same layer id and metadata). Raises KeyError if the layer is missing or empty.
    """
    layer = document.layers.get(layer_id)
    if layer is None or layer.is_empty():
        raise KeyError(layer_id)
    single = vpype.Document(page_size=document.page_size)
    single.add(layer, layer_id=layer_id, with_metadata=True)
    return single


def read_document(svg_filepath):
    """
    Reads an SVG into a vpype.Document, one layer per SVG layer, curves flattened
    and transforms applied.
    """
    return vpype.read_multilayer_svg(svg_filepath, quantization=vpype.convert_length("0.1mm"))


def _rgb(color):
    if color is None:
        return None
    if hasattr(color, "red"):
        return color.red, color.green, color.blue
    return vpype.Color(color).red, vpype.Color(color).green, vpype.Color(color).blue


def match_pen(rgb, pens=None):
    """
    Returns the name of the pen closest to an (R, G, B) tuple, or "Unassigned"
    if rgb is None.
    """
    if rgb is None:
        return "Unassigned"
    pens = DEFAULT_PENS if pens is None else pens
    return min(pens, key=lambda n: sum((a - b) ** 2 for a, b in zip(rgb, pens[n])))


def _clean(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(text)).strip("_") or "layer"


def split_layers(svg_filepath, output_dir="layerfile", pens=None, group_by_pen=False):
    """
    Splits a multi-layer SVG into one SVG per layer in output_dir, preserving the
    page size, and returns a list of LayerInfo (one per non-empty layer).

    With group_by_pen=True, layers that match the same pen are merged into one
    layer/file, so each pen is loaded once and the paths can be optimized together.
    Groups keep the order in which each pen first appears.
    """
    os.makedirs(output_dir, exist_ok=True)
    doc = read_document(svg_filepath)

    # (name, pen, hex, LineCollection) per non-empty layer, in layer order
    found = []
    for lid in sorted(doc.layers):
        layer = doc.layers[lid]
        if layer.is_empty():
            continue
        name, pen, hex_color = _describe(layer, lid, pens)
        found.append([name, pen, hex_color, layer])

    if group_by_pen:
        grouped = {}
        for name, pen, hex_color, layer in found:
            if pen in grouped:
                grouped[pen][0] += f", {name}"
                grouped[pen][3] = _merged(grouped[pen][3], layer)
            else:
                grouped[pen] = [name, pen, hex_color, layer]
        found = list(grouped.values())

    layers = []
    for index, (name, pen, hex_color, layer) in enumerate(found):
        single = vpype.Document(page_size=doc.page_size)
        single.add(layer, layer_id=1, with_metadata=True)
        filename = os.path.join(output_dir, f"{index:02d}_{pen}_{_clean(name)[:40]}_{hex_color.lstrip('#')}.svg")
        with open(filename, "w") as f:
            vpype.write_svg(f, single, page_size=single.page_size)
        layers.append(LayerInfo(index, name, pen, hex_color, filename))
    return layers


def _merged(a, b):
    out = vpype.LineCollection(a)  # clone() would drop the lines, so copy via the constructor
    out.extend(b)
    return out
