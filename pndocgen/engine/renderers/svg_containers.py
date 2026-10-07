"""Fit explicit leaf columns to measured labels without moving nodes or edges.

Nested ancestors may expand in the same transaction. Container endpoints,
unknown text and unsafe route/neighbor intersections cause an explicit refusal.
"""
from pathlib import Path
import statistics

from pndocgen.engine.renderers.svg_geometry import Diagram, EPS, NS, rect_box
from pndocgen.engine.renderers.svg_normalizer import _serialize, _reload
from pndocgen.engine.renderers.svg_text import TextMetrics, union
from pndocgen.engine.renderers.svg_alignment import targets, Rule


def contains(outer, inner, margin=0):
    return (inner[0] >= outer[0] + margin - EPS and inner[1] >= outer[1] + margin - EPS
            and inner[0]+inner[2] <= outer[0]+outer[2]-margin+EPS
            and inner[1]+inner[3] <= outer[1]+outer[3]-margin+EPS)


def overlaps(a, b):
    return (a[0] < b[0]+b[2]-EPS and b[0] < a[0]+a[2]-EPS
            and a[1] < b[1]+b[3]-EPS and b[1] < a[1]+a[3]-EPS)


def visual(node, boxes):
    return union([node.box] + [boxes[t] for t in node.texts])


def text_audit(diagram):
    boxes, unknown = TextMetrics(diagram.tree.getroot()).measure(diagram)
    overflow = []
    for node in diagram.nodes.values():
        if node.cluster_id in diagram.containers and all(t in boxes for t in node.texts):
            if not contains(diagram.containers[node.cluster_id].box, visual(node, boxes)):
                overflow.append(node.node_id)
    return {"overflow": sorted(overflow), "unmeasured": unknown}, boxes


def text_conflicts(diagram):
    """Stable conservative collision identities for node/container labels."""
    audit, boxes = text_audit(diagram)
    if audit['unmeasured']:
        raise ValueError('Cannot verify collisions with unmeasured text')
    labels = [(f'{key}:{i}', key, boxes[text])
              for key,node in sorted({**diagram.nodes, **diagram.containers}.items())
              for i,text in enumerate(node.texts)]
    issues = set()
    for i,(identity,owner,box) in enumerate(labels):
        for edge in diagram.edges:
            if edge.label is not None and overlaps(box, boxes[edge.label]):
                issues.add(('text-edge-label', identity, edge.edge_id))
        for key,node in diagram.nodes.items():
            if overlaps(box,node.box):
                issues.add(('text-node',identity,key))
        for other,_,other_box in labels[i+1:]:
            if overlaps(box,other_box):
                issues.add(('text-text',identity,other))
        for edge in diagram.edges:
            if not edge.path.supported:
                raise ValueError('Cannot verify text corridors for unsupported paths')
            last = edge.path.commands[0].anchor
            for index,command in enumerate(edge.path.commands[1:]):
                points = [last]+list(zip(command.values[::2],command.values[1::2]))
                hull = union([(x-1,y-1,2,2) for x,y in points])
                if overlaps(box,hull):
                    issues.add(('text-path',identity,edge.edge_id,index))
                last=command.anchor
    return issues


def measured_text_gate(before, after):
    """Reject new measured text defects; existing defects are not silently fixed."""
    if any(element.get(attribute)
           for diagram in (before, after)
           for element in diagram.tree.getroot().iter()
           for attribute in ('transform', 'filter', 'clip-path')):
        return 'unsupported SVG effects prevent safe layout edits'
    old_audit, _ = text_audit(before)
    new_audit, _ = text_audit(after)
    if old_audit['unmeasured'] or new_audit['unmeasured']:
        return 'unmeasured text prevents safe layout edits'
    if set(new_audit['overflow']) - set(old_audit['overflow']):
        return 'new text overflow'
    try:
        if text_conflicts(after) - text_conflicts(before):
            return 'new text collision'
    except ValueError as exc:
        return str(exc)
    return None


def _related(a, b):
    return a == b or a.startswith(b + '.') or b.startswith(a + '.')


def _set_box(node, box):
    for attr, value in zip(('x', 'y', 'width', 'height'), box):
        node.rect.set(attr, f'{value:.6f}')


def _check(before, after, boxes_before, boxes_after, changed):
    old_edges = {e.edge_id: e for e in before.edges}
    for key in changed:
        container = after.containers[key]
        if after.incident(key):
            return 'container has attached edges'
        for other_key, other in after.containers.items():
            if not _related(key, other_key) and overlaps(container.box, other.box):
                return 'would overlap an unrelated container'
        for node in after.nodes.values():
            if not node.node_id.startswith(key + '.') and overlaps(container.box, visual(node, boxes_after)):
                return 'would overlap an unrelated node or text'
        for title in container.texts:
            if not contains(container.box, boxes_after[title]):
                return 'title outside its container'
            if any(overlaps(boxes_after[title], visual(n, boxes_after))
                   for n in after.nodes.values() if n.node_id.startswith(key + '.')):
                return 'title would overlap child content'
            for edge in after.edges:
                if not edge.path.supported:
                    return 'unsupported edge near moved title'
                last = edge.path.commands[0].anchor
                for command in edge.path.commands[1:]:
                    points = [last] + list(zip(command.values[::2], command.values[1::2]))
                    hull = union([(x-1, y-1, 2, 2) for x, y in points])
                    if overlaps(boxes_after[title], hull):
                        return 'moved title would overlap an edge'
                    last = command.anchor
        for edge in after.edges:
            if edge.source.startswith(key + '.') or edge.target.startswith(key + '.'):
                continue
            if not edge.path.supported:
                return 'unsupported unrelated edge path'
            last = edge.path.commands[0].anchor
            for command in edge.path.commands[1:]:
                points = [last] + list(zip(command.values[::2], command.values[1::2]))
                hull = union([(x-1, y-1, 2, 2) for x, y in points])
                # Preserve preexisting passages; never add a new intersection.
                if overlaps(container.box, hull) and not overlaps(before.containers[key].box, hull):
                    return 'new unrelated edge corridor inside container'
                last = command.anchor
            if edge.label is not None and overlaps(container.box, boxes_after[edge.label]):
                if not overlaps(before.containers[key].box, boxes_before[old_edges[edge.edge_id].label]):
                    return 'new unrelated edge label inside container'
    return None


def fit_containers(source: Path, destination: Path, contracts, *, margin=16.0, max_growth=240.0,
                   original_widths=None):
    import math
    if not all(math.isfinite(v) and v >= 0 for v in (margin, max_growth)):
        raise ValueError('Container fitting limits must be finite and nonnegative')
    if destination.exists() or destination.resolve() == source.resolve():
        raise FileExistsError(destination)
    diagram = Diagram.load(source)
    audit, boxes = text_audit(diagram)
    report = {'before': audit, 'applied': [], 'skipped': []}
    unsupported = any(e.get(a) for e in diagram.tree.getroot().iter()
                      for a in ('transform', 'filter', 'clip-path'))
    if audit['unmeasured'] or unsupported:
        report['skipped'].append({'cluster': '*', 'reason': 'unmeasured text or unsupported SVG effects'})
    else:
        for key, rule in sorted((contracts or {}).items()):
            if rule.get('layout') != 'column' or key not in diagram.containers:
                continue
            nodes = diagram.children_of(key)
            if not nodes or any(c.cluster_id == key for c in diagram.containers.values()):
                continue
            try:
                targets(diagram, key, Rule('free'))
            except ValueError as exc:
                report['skipped'].append({'cluster': key, 'reason': str(exc)})
                continue
            # Alignment may conservatively abstain. Fit the actual content
            # envelope even for a ragged column, without moving its children.
            # The same growth, route and neighbor guards still apply below.
            snapshot = _serialize(diagram)
            candidate = _reload(snapshot, source)
            c = candidate.containers[key]
            axis = statistics.median(n.center[0] for n in nodes)
            contents = union(visual(n, boxes) for n in nodes)
            half = max(axis-contents[0], contents[0]+contents[2]-axis,
                       *(boxes[t][2]/2 for t in diagram.containers[key].texts),
                       (original_widths or {}).get(key, c.box[2])/2-margin) + margin
            proposed = (axis-half, c.box[1], half*2, c.box[3])
            reason = None
            if proposed[2] > c.box[2] + max_growth + EPS:
                reason = 'width growth exceeds limit'
            elif not contains(proposed, contents, margin):
                reason = 'vertical content requires relayout'
            if reason:
                report['skipped'].append({'cluster': key, 'reason': reason})
                continue
            if all(abs(a-b) <= EPS for a,b in zip(c.box, proposed)):
                continue
            _set_box(c, proposed)
            for title in c.texts:
                title.set('x', f'{axis:.6f}')
            changed = [key]
            # Propagate only expansion, not repositioning of ancestor contents.
            while c.cluster_id in candidate.containers:
                parent = candidate.containers[c.cluster_id]
                if not contains(parent.box, c.box, margin):
                    expanded = union([parent.box, (c.box[0]-margin, c.box[1]-margin,
                                                  c.box[2]+2*margin, c.box[3]+2*margin)])
                    if expanded[2] > parent.box[2]+max_growth+EPS or expanded[3] > parent.box[3]+max_growth+EPS:
                        reason = 'ancestor growth exceeds limit'
                        break
                    _set_box(parent, expanded)
                    for title in parent.texts:
                        title.set('x', f'{parent.center[0]:.6f}')
                    changed.append(parent.node_id)
                c = parent
            _, proposed_boxes = text_audit(candidate)
            reason = reason or _check(diagram, candidate, boxes, proposed_boxes, changed)
            reason = reason or measured_text_gate(diagram, candidate)
            if reason:
                report['skipped'].append({'cluster': key, 'reason': reason})
                continue
            report['applied'].append({'cluster': key, 'before': diagram.containers[key].box,
                                      'after': candidate.containers[key].box, 'changed': changed})
            diagram, boxes = candidate, proposed_boxes
    # Expand viewports only if the moved boundaries need additional space.
    root = diagram.tree.getroot()
    inner = root.findall(NS + 'svg')
    viewport = inner[0] if len(inner) == 1 else root
    old = tuple(float(v) for v in viewport.get('viewBox').split())
    required = union([old] + [(c.box[0]-8, c.box[1]-8, c.box[2]+16, c.box[3]+16)
                             for c in diagram.containers.values()])
    if report['applied'] and not contains(old, required):
        viewport.set('viewBox', ' '.join(f'{v:.6f}' for v in required))
        if viewport is not root:
            root.set('viewBox', f'0 0 {required[2]:.6f} {required[3]:.6f}')
            viewport.set('width', f'{required[2]:.6f}')
            viewport.set('height', f'{required[3]:.6f}')
        root.set('width', f'{required[2]:.6f}')
        root.set('height', f'{required[3]:.6f}')
        for rect in viewport.findall(NS + 'rect'):
            if contains(rect_box(rect), old):
                for attr, value in zip(('x', 'y', 'width', 'height'), required):
                    rect.set(attr, f'{value:.6f}')
    report['after'], _ = text_audit(diagram)
    if set(report['after']['overflow']) - set(report['before']['overflow']):
        raise ValueError('Container fitting introduced text overflow')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as stream:
        stream.write(_serialize(diagram) if report['applied'] else source.read_bytes())
    return report
