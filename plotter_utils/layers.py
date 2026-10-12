import os
import re
from dataclasses import dataclass

import vpype

from .config import DEFAULT_PENS

__all__ = ["LayerInfo", "match_pen", "split_layers", "read_document"]


@dataclass
class LayerInfo:
    index: int
    name: str
    pen: str
    hex_color: str
    filename: str


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


def split_layers(svg_filepath, output_dir="layerfile", pens=None):
    """
    Splits a multi-layer SVG into one SVG per layer in output_dir, preserving the
    page size, and returns a list of LayerInfo (one per non-empty layer).
    """
    os.makedirs(output_dir, exist_ok=True)
    doc = read_document(svg_filepath)

    layers = []
    for index, lid in enumerate(sorted(doc.layers)):
        layer = doc.layers[lid]
        if layer.is_empty():
            continue
        color = layer.property(vpype.METADATA_FIELD_COLOR) if layer.property_exists(vpype.METADATA_FIELD_COLOR) else None
        name = layer.property(vpype.METADATA_FIELD_NAME) if layer.property_exists(vpype.METADATA_FIELD_NAME) else f"layer{lid}"
        rgb = _rgb(color)
        pen = match_pen(rgb, pens)
        hex_color = "#%02x%02x%02x" % rgb if rgb else ""

        single = vpype.Document(page_size=doc.page_size)
        single.add(layer, layer_id=1, with_metadata=True)
        filename = os.path.join(output_dir, f"{index:02d}_{pen}_{_clean(name)}_{hex_color.lstrip('#')}.svg")
        with open(filename, "w") as f:
            vpype.write_svg(f, single, page_size=single.page_size)
        layers.append(LayerInfo(index, name, pen, hex_color, filename))
    return layers
