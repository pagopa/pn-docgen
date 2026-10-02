"""Bounded horizontal container-title placement, without moving any routes.

Uses measured obstacles to enumerate interval boundaries, not guessed offsets
or component-specific rules. A title already free of conflicts is untouched.
"""
import math
from pathlib import Path
from pndocgen.engine.renderers.svg_geometry import Diagram, EPS
from pndocgen.engine.renderers.svg_containers import text_audit, text_conflicts, contains, overlaps
from pndocgen.engine.renderers.svg_normalizer import _reload, _serialize
from pndocgen.engine.renderers.svg_text import union


def separate_container_titles(source, destination, *, max_shift=120, margin=16, clearance=2):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve() or destination.exists():
        raise FileExistsError(destination)
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0
           for v in (max_shift, margin, clearance)):
        raise ValueError('Title placement limits must be finite and nonnegative')
    diagram = Diagram.load(source)
    audit, boxes = text_audit(diagram)
    report = {'applied': [], 'skipped': []}
    if audit['unmeasured'] or any(not e.path.supported for e in diagram.edges) or any(e.get(a) for e in diagram.tree.getroot().iter()
                                for a in ('transform', 'filter', 'clip-path')):
        report['skipped'].append(('*', 'unmeasured text or unsupported SVG effects'))
    else:
        conflicts = text_conflicts(diagram)
        report['before'] = sorted(conflicts)
        for key in sorted(diagram.containers):
            for index in range(len(diagram.containers[key].texts)):
                identity = f'{key}:{index}'
                if not any(identity in c[1:3] for c in conflicts):
                    continue
                container = diagram.containers[key]
                title = container.texts[index]
                box = boxes[title]
                obstacles = [bounds for text, bounds in boxes.items() if text is not title]
                obstacles.extend(node.box for node in diagram.nodes.values())
                unsupported = False
                for edge in diagram.edges:
                    if not edge.path.supported:
                        unsupported = True
                        break
                    last = edge.path.commands[0].anchor
                    for command in edge.path.commands[1:]:
                        points = [last] + list(zip(command.values[::2], command.values[1::2]))
                        obstacles.append(union((x - 1, y - 1, 2, 2) for x, y in points))
                        last = command.anchor
                if unsupported:
                    report['skipped'].append((identity, 'unsupported edge path'))
                    continue
                low = max(-max_shift, container.box[0] + margin - box[0])
                high = min(max_shift, container.box[0] + container.box[2] - margin - box[0] - box[2])
                offsets = {low, high}
                for obstacle in obstacles:
                    if (box[1] < obstacle[1] + obstacle[3] + clearance and
                            obstacle[1] - clearance < box[1] + box[3]):
                        offsets.update((obstacle[0] - clearance - box[0] - box[2],
                                        obstacle[0] + obstacle[2] + clearance - box[0]))
                accepted = False
                for offset in sorted(offsets, key=lambda value: (abs(value), value)):
                    if offset < low - EPS or offset > high + EPS or abs(offset) < EPS:
                        continue
                    candidate = _reload(_serialize(diagram), source)
                    moved = candidate.containers[key].texts[index]
                    moved.set('x', f"{float(moved.get('x')) + offset:.6f}")
                    new_audit, new_boxes = text_audit(candidate)
                    if new_audit['unmeasured'] or not contains(container.box, new_boxes[moved]):
                        continue
                    if any(overlaps(new_boxes[moved],
                                    (b[0] - clearance, b[1] - clearance,
                                     b[2] + 2 * clearance, b[3] + 2 * clearance))
                           for b in obstacles):
                        continue
                    new_conflicts = text_conflicts(candidate)
                    if (not new_conflicts.issubset(conflicts) or
                            any(identity in c[1:3] for c in new_conflicts)):
                        continue
                    diagram, boxes, conflicts = candidate, new_boxes, new_conflicts
                    report['applied'].append({'title': identity, 'dx': offset})
                    accepted = True
                    break
                if not accepted:
                    report['skipped'].append((identity, 'no safe horizontal interval within limits'))
        report['after'] = sorted(conflicts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as stream:
        stream.write(_serialize(diagram) if report['applied'] else source.read_bytes())
    return report
