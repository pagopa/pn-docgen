"""Fit a square image to an overfull, already routed bundle with bounded edits.

Never rescales image bytes or changes routing topology. Only enabled classes
are eligible. Standard aws_node icons remain uniform by default.
"""
import math
from pathlib import Path
import statistics
from pndocgen.engine.renderers.svg_geometry import Diagram, EPS, find_port_spans, collisions, non_orthogonal_segments
from pndocgen.engine.renderers.svg_normalizer import _serialize, _reload, _valid, _attachments, _label_distances, _shift_endpoint
from pndocgen.engine.renderers.svg_alignment import _straight_direction_checks, _port_offsets
from pndocgen.engine.renderers.svg_containers import text_audit, text_conflicts


def fit_ports(source, destination, *, allowed_classes=('ecs_hero',), max_growth=24, max_shift=30,
              padding=2, contracts=None, min_gap=24):
    if any(type(v) not in (float,int) or not math.isfinite(v) or v<0 for v in (max_growth,max_shift,padding,min_gap)):
        raise ValueError('Port capacity limits must be finite and nonnegative')
    if source.resolve() == destination.resolve() or destination.exists():
        raise FileExistsError(destination)
    before = Diagram.load(source)
    report = {'applied':[], 'skipped':[]}
    if any(e.get(a) for e in before.tree.getroot().iter() for a in ('transform','filter','clip-path')):
        report['skipped'].append(('*','unsupported SVG effects/transforms'))
        with destination.open('xb') as stream:
            stream.write(source.read_bytes())
        return report
    for node_id in sorted({s.node_id for s in find_port_spans(before)}):
        old_node = before.nodes[node_id]
        if old_node.image is None or not set(old_node.classes).intersection(allowed_classes):
            continue
        incident = before.incident(node_id)
        axes = {e.terminal_axis(at_source) for e,at_source in incident}
        if len(axes) != 1 or None in axes or any(not e.path.supported or e.source == e.target for e,_ in incident):
            report['skipped'].append((node_id, 'requires one supported terminal axis, no self-loop'))
            continue
        axis = next(iter(axes))
        index = 1 if axis == 'x' else 0
        siblings=before.children_of(old_node.cluster_id)
        rule=(contracts or {}).get(old_node.cluster_id,{})
        if abs(old_node.box[2]-old_node.box[3])>EPS or (len(siblings)>1 and
                rule.get('layout') != ('column' if axis=='x' else 'row')):
            report['skipped'].append((node_id,'requires square image and explicit free-axis contract'))
            continue
        spans = [s for s in find_port_spans(before) if s.node_id == node_id]
        if not spans or max(s.offset for s in spans)-min(s.offset for s in spans)>EPS:
            report['skipped'].append((node_id, 'bundle centers disagree'))
            continue
        delta = statistics.median(s.offset for s in spans)
        center = old_node.center[index]+delta
        points = [(e.start if at_source else e.end)[index] for e,at_source in incident]
        size = max(old_node.box[2],old_node.box[3], math.ceil(2*max(abs(v-center) for v in points)+2*padding))
        if size <= max(old_node.box[2:]) + EPS and abs(delta) <= EPS:
            continue
        if size-max(old_node.box[2:])>max_growth or abs(delta)>max_shift:
            report['skipped'].append((node_id, 'capacity exceeds bounded growth/shift'))
            continue
        after = _reload(_serialize(before), source)
        node = after.nodes[node_id]
        x,y,w,h = node.box
        dx,dy = (0,delta) if axis=='x' else (delta,0)
        node.translate(dx,dy)
        for attr,value in [('x',x+dx-(size-w)/2),('y',y+dy-(size-h)/2),('width',size),('height',size)]:
            node.image.set(attr,f'{value:.6f}')
        # D2 image labels are outside-bottom-center. Refuse other placements.
        if any(float(t.get('y',0)) < y+h for t in old_node.texts):
            report['skipped'].append((node_id,'not a bottom-label contract'))
            continue
        for text in node.texts:
            text.set('y',f"{float(text.get('y'))+(size-h)/2:.6f}")
        for edge,at_source in after.incident(node_id):
            point = edge.start if at_source else edge.end
            positive = point[0 if axis=='x' else 1] >= old_node.center[0 if axis=='x' else 1]
            growth = (size-(w if axis=='x' else h))/2
            _shift_endpoint(edge,at_source,axis,growth if positive else -growth)
        failure = _valid(after,_attachments(before),non_orthogonal_segments(before),collisions(before),_label_distances(before))
        failure = failure or _straight_direction_checks(before,after,10)
        old_order=sorted(siblings,key=lambda n:(n.center[index],n.node_id))
        new_order=sorted(after.children_of(old_node.cluster_id),key=lambda n:(n.center[index],n.node_id))
        if [n.node_id for n in old_order] != [n.node_id for n in new_order]:
            failure=failure or 'sibling order changed'
        for a,b in zip(old_order,old_order[1:]):
            old_gap=b.box[index]-a.box[index]-a.box[index+2]
            na,nb=after.nodes[a.node_id],after.nodes[b.node_id]
            new_gap=nb.box[index]-na.box[index]-na.box[index+2]
            if new_gap<min(old_gap,min_gap)-EPS:
                failure=failure or 'sibling gap below safety floor'
        old_ports = _port_offsets(before)
        if any(v>old_ports.get(k,v)+EPS for k,v in _port_offsets(after).items()):
            failure = failure or 'existing bundle worsened'
        old_audit,_ = text_audit(before)
        new_audit,_ = text_audit(after)
        if new_audit['unmeasured'] or set(new_audit['overflow'])-set(old_audit['overflow']):
            failure = failure or 'new text overflow/unmeasured text'
        try:
            if text_conflicts(after)-text_conflicts(before):
                failure = failure or 'new text collision'
        except ValueError as exc:
            failure = failure or str(exc)
        if failure:
            report['skipped'].append((node_id,failure))
        else:
            report['applied'].append({'node':node_id,'size_before':old_node.box[2:],'size_after':size,'shift':delta})
            before=after
    with destination.open('xb') as stream:
        stream.write(_serialize(before) if report['applied'] else source.read_bytes())
    return report
