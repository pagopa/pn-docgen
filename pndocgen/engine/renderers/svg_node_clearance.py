"""Move colliding bottom labels down within their own container, preserving x."""
import math
from pathlib import Path
from pndocgen.engine.renderers.svg_geometry import Diagram, EPS
from pndocgen.engine.renderers.svg_containers import text_audit, text_conflicts, contains, overlaps
from pndocgen.engine.renderers.svg_normalizer import _reload, _serialize
from pndocgen.engine.renderers.svg_text import union
from pndocgen.engine.renderers.svg_preferences import _gate


def separate_node_labels(source, destination, *, max_shift=120, max_node_shift=120, gap=4, clearance=2):
    source, destination = Path(source), Path(destination)
    if destination.exists() or source.resolve() == destination.resolve():
        raise FileExistsError(destination)
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0
           for v in (max_shift, max_node_shift, gap, clearance)):
        raise ValueError('Node label clearance limits must be finite and nonnegative')
    diagram = Diagram.load(source)
    report = {'applied': [], 'skipped': []}
    audit, boxes = text_audit(diagram)
    if (audit['unmeasured'] or any(not e.path.supported for e in diagram.edges)
            or any(e.get(a) for e in diagram.tree.getroot().iter() for a in ('transform', 'filter', 'clip-path'))):
        report['skipped'].append(('*', 'unsupported SVG geometry or text'))
    else:
        conflicts = text_conflicts(diagram)
        for key in sorted(diagram.nodes):
            node = diagram.nodes[key]
            identity = f'{key}:0'
            if len(node.texts) != 1 or not any(identity in c[1:3] for c in conflicts):
                continue
            text = node.texts[0]
            box = boxes[text]
            if (node.cluster_id not in diagram.containers or 'text-anchor:middle' not in text.get('style', '')
                    or box[1] < node.box[1] + node.box[3] - EPS):
                report['skipped'].append((key, 'not an owned bottom label'))
                continue
            obstacles = [b for t, b in boxes.items() if t is not text]
            obstacles.extend(n.box for n in diagram.nodes.values())
            for edge in diagram.edges:
                last = edge.path.commands[0].anchor
                for command in edge.path.commands[1:]:
                    obstacles.append(union((x-1, y-1, 2, 2)
                        for x, y in [last] + list(zip(command.values[::2], command.values[1::2]))))
                    last = command.anchor
            if not any(e.source == key or e.target == key for e in diagram.edges):
                offsets_x = set()
                for obstacle in obstacles:
                    if box[1] < obstacle[1] + obstacle[3] + clearance and box[1]+box[3] > obstacle[1]-clearance:
                        offsets_x.update((obstacle[0]-clearance-box[0]-box[2],
                                          obstacle[0]+obstacle[2]+clearance-box[0]))
                moved_free = False
                for dx in sorted(offsets_x, key=lambda x: (abs(x), x)):
                    if not EPS < abs(dx) <= max_node_shift:
                        continue
                    candidate = _reload(_serialize(diagram), source)
                    candidate.nodes[key].translate(dx, 0)
                    if _gate(diagram, candidate):
                        continue
                    after = text_conflicts(candidate)
                    if any(identity in c[1:3] for c in after):
                        continue
                    diagram, conflicts = candidate, after
                    audit, boxes = text_audit(diagram)
                    report['applied'].append({'node': key, 'dx': dx, 'mode': 'unconnected node and label'})
                    moved_free = True
                    break
                if moved_free:
                    continue
            offsets = {node.box[1] + node.box[3] + gap - box[1]}
            for obstacle in obstacles:
                if box[0] < obstacle[0] + obstacle[2] + clearance and box[0] + box[2] > obstacle[0] - clearance:
                    offsets.add(obstacle[1] + obstacle[3] + clearance - box[1])
            accepted = False
            for dy in sorted(v for v in offsets if EPS < v <= max_shift):
                moved_box = (box[0], box[1]+dy, box[2], box[3])
                if not contains(diagram.containers[node.cluster_id].box, moved_box, margin=clearance):
                    continue
                # Do not let a label drift into the next node's vertical band.
                if any(other.cluster_id == node.cluster_id and other.node_id != key
                       and other.box[1] > node.box[1]
                       and moved_box[1] + moved_box[3] + gap > other.box[1]
                       for other in diagram.nodes.values()):
                    continue
                if any(overlaps(moved_box, (b[0]-clearance, b[1]-clearance, b[2]+2*clearance, b[3]+2*clearance)) for b in obstacles):
                    continue
                candidate = _reload(_serialize(diagram), source)
                moved = candidate.nodes[key].texts[0]
                moved.set('y', f'{float(moved.get("y"))+dy:.6f}')
                new_audit, new_boxes = text_audit(candidate)
                after = text_conflicts(candidate)
                if (new_audit['unmeasured'] or set(new_audit['overflow']) - set(audit['overflow'])
                        or not after.issubset(conflicts) or any(identity in c[1:3] for c in after)):
                    continue
                diagram, audit, boxes, conflicts = candidate, new_audit, new_boxes, after
                report['applied'].append({'node': key, 'dy': dy})
                accepted = True
                break
            if not accepted:
                report['skipped'].append((key, 'no safe bottom interval within limits'))
    with destination.open('xb') as stream:
        stream.write(_serialize(diagram) if report['applied'] else source.read_bytes())
    return report
