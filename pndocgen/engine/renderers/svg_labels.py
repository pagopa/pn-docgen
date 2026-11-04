"""Move overlapping labels along their existing straight segment, never paths."""
from pathlib import Path
import math

from pndocgen.engine.renderers.svg_geometry import Diagram, EPS, collisions, label_segment, rect_box, _intersects
from pndocgen.engine.renderers.svg_normalizer import _serialize, _reload
from pndocgen.engine.renderers.svg_preferences import _gate, _save


def separate_labels(source: Path, destination: Path, max_shift=120.0):
    if type(max_shift) not in (int, float) or not math.isfinite(max_shift) or max_shift < 0:
        raise ValueError("max label shift must be finite and nonnegative")
    if destination.exists():
        raise FileExistsError(destination)
    diagram = Diagram.load(source)
    original_boxes = {e.edge_id: rect_box(e.label_mask) for e in diagram.edges if e.label_mask is not None}
    report = {"applied": [], "skipped": []}
    if any(e.get("transform") for e in diagram.tree.getroot().iter()):
        report["skipped"].append({"reason": "unsupported transform"})
        _save(source, destination, diagram, False)
        return report
    edge_ids = sorted({edge_id for kind, a, b in collisions(diagram)
                       if kind == "label-label" for edge_id in (a, b)})
    for edge_id in edge_ids:
        old_conflicts = collisions(diagram)
        if not any(kind == "label-label" and edge_id in (a, b) for kind, a, b in old_conflicts):
            continue
        edge = next(e for e in diagram.edges if e.edge_id == edge_id)
        located = label_segment(edge)
        if located is None:
            report["skipped"].append({"edge": edge_id, "reason": "no exact label mask/segment"})
            continue
        index, distance = located
        a, b = edge.path.commands[index-1].anchor, edge.path.commands[index].anchor
        if abs(a[1]-b[1]) <= EPS and abs(a[0]-b[0]) > EPS:
            axis = 0
        elif abs(a[0]-b[0]) <= EPS and abs(a[1]-b[1]) > EPS:
            axis = 1
        else:
            continue
        before = _serialize(diagram)
        accepted = False
        for magnitude in range(6, int(max_shift)+1, 6):
            for delta in (-magnitude, magnitude):
                candidate = _reload(before, source)
                current = next(e for e in candidate.edges if e.edge_id == edge_id)
                box = rect_box(current.label_mask)
                low, high = sorted((a[axis], b[axis]))
                if box[axis]+delta < low+6 or box[axis]+box[axis+2]+delta > high-6:
                    continue
                attr = "x" if axis == 0 else "y"
                for element in (current.label, current.label_mask):
                    element.set(attr, f"{float(element.get(attr))+delta:.6f}")
                remaining = collisions(candidate)
                new_segment = label_segment(current)
                if (remaining - old_conflicts or len(remaining) >= len(old_conflicts)
                        or new_segment is None or new_segment[0] != index
                        or new_segment[1] > distance+EPS or _gate(diagram, candidate)):
                    continue
                diagram = candidate
                report["applied"].append({"edge": edge_id, "axis": attr, "delta": delta})
                accepted = True
                break
            if accepted:
                break
        if not accepted:
            report["skipped"].append({"edge": edge_id, "reason": "no safe position on original segment"})
    # A crowded pair can have a safe joint placement even when neither label
    # can move alone. Commit both moves atomically under the same geometry gate.
    for kind, first, second in sorted(collisions(diagram)):
        if kind != 'label-label' or (kind, first, second) not in collisions(diagram):
            continue
        choices = []
        for edge_id in (first, second):
            edge = next(e for e in diagram.edges if e.edge_id == edge_id)
            located = label_segment(edge)
            options = []
            if located:
                index, distance = located
                a, b = edge.path.commands[index-1].anchor, edge.path.commands[index].anchor
                axis = 0 if abs(a[1]-b[1]) <= EPS else 1 if abs(a[0]-b[0]) <= EPS else None
                if axis is not None:
                    box = rect_box(edge.label_mask)
                    low, high = sorted((a[axis], b[axis]))
                    for magnitude in range(6, int(max_shift)+1, 6):
                        for delta in (-magnitude, magnitude):
                            moved = list(box)
                            moved[axis] += delta
                            if abs(moved[axis] - original_boxes[edge_id][axis]) > max_shift:
                                continue
                            if moved[axis] < low+6 or moved[axis]+moved[axis+2] > high-6:
                                continue
                            if any(_intersects(moved, rect_box(other.label_mask))
                                   for other in diagram.edges if other.label_mask is not None
                                   and other.edge_id not in (first, second)):
                                continue
                            options.append((delta, axis, index, distance, moved))
            choices.append(options)
        pairs = [(a, b) for a in choices[0] for b in choices[1] if not _intersects(a[4], b[4])]
        pairs.sort(key=lambda pair: (sum(abs(v[0]) for v in pair), max(abs(v[0]) for v in pair),
                                     pair[0][0], pair[1][0]))
        before = _serialize(diagram)
        old_conflicts = collisions(diagram)
        for pair in pairs:
            candidate = _reload(before, source)
            valid = True
            for edge_id, (delta, axis, index, distance, _) in zip((first, second), pair):
                edge = next(e for e in candidate.edges if e.edge_id == edge_id)
                attr = 'x' if axis == 0 else 'y'
                for element in (edge.label, edge.label_mask):
                    element.set(attr, f'{float(element.get(attr))+delta:.6f}')
                located = label_segment(edge)
                valid = valid and located is not None and located[0] == index and located[1] <= distance+EPS
            remaining = collisions(candidate)
            if (not valid or remaining - old_conflicts or len(remaining) >= len(old_conflicts)
                    or _gate(diagram, candidate)):
                continue
            diagram = candidate
            for edge_id, (delta, axis, _, _, _) in zip((first, second), pair):
                report['applied'].append({'edge': edge_id, 'axis': 'x' if axis == 0 else 'y',
                                          'delta': delta, 'paired': True})
            report['skipped'] = [r for r in report['skipped'] if r.get('edge') not in (first, second)]
            break
    _save(source, destination, diagram, bool(report["applied"]))
    return report
