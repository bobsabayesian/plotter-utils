import copy

import vpype
import vpype_cli

__all__ = ["optimize_document", "optimization_report"]


def optimize_document(document, merge=True, sort=True, reloop=True, simplify=True,
                      merge_tolerance="0.1mm", simplify_tolerance="0.05mm"):
    """
    Returns a new vpype.Document with paths merged, reordered and simplified to cut
    pen-up travel and drawing time. The input document is not modified.

    merge     joins paths whose ends are within merge_tolerance (fewer pen lifts)
    sort      reorders paths to shorten pen-up travel
    reloop    starts closed paths at the point nearest the previous path
    simplify  drops points that deviate less than simplify_tolerance
    """
    stages = []
    if merge:
        stages.append(f"linemerge --tolerance {merge_tolerance}")
    if sort:
        stages.append("linesort")
    if reloop:
        stages.append("reloop")
    if simplify:
        stages.append(f"linesimplify --tolerance {simplify_tolerance}")
    if not stages:
        return copy.deepcopy(document)
    # clone() would give an empty document, so deep-copy to leave the input intact
    return vpype_cli.execute(" ".join(stages), copy.deepcopy(document))


def optimization_report(before, after):
    """
    Compares two documents. Lengths are in mm; 'paths' counts pen-down strokes.
    """
    px_per_mm = vpype.convert_length("1mm")

    def stats(doc):
        travel = doc.pen_up_length()
        if isinstance(travel, tuple):  # some vpype versions return (total, mean)
            travel = travel[0]
        return {
            "paths": sum(len(lc) for lc in doc.layers.values()),
            "draw_mm": doc.length() / px_per_mm,
            "travel_mm": travel / px_per_mm,
        }

    b, a = stats(before), stats(after)
    return {"before": b, "after": a}
