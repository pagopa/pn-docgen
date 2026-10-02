"""Keep plain bottom labels at a measured gap after image/grid normalization."""
import math

from pndocgen.engine.renderers.svg_geometry import Diagram, EPS
from pndocgen.engine.renderers.svg_normalizer import _serialize, _reload
from pndocgen.engine.renderers.svg_containers import text_audit, text_conflicts


def align_node_labels(source,destination,*,gap=4.0):
    if type(gap) not in (int,float) or not math.isfinite(gap) or gap<0:
        raise ValueError('Node label gap must be finite and nonnegative')
    if destination.exists() or destination.resolve()==source.resolve():
        raise FileExistsError(destination)
    diagram=Diagram.load(source)
    report={'applied':[],'skipped':[]}
    unsupported=any(e.get(a) for e in diagram.tree.getroot().iter() for a in ('transform','filter','clip-path'))
    audit,boxes=text_audit(diagram)
    if unsupported or audit['unmeasured']:
        report['skipped'].append({'node':'*','reason':'unsupported geometry/text'})
    else:
        for key in sorted(diagram.nodes):
            node=diagram.nodes[key]
            if node.image is None or len(node.texts)!=1:
                continue
            label=node.texts[0]
            if 'text-anchor:middle' not in label.get('style','') or float(label.get('y',0))<node.box[1]+node.box[3]:
                continue
            delta=node.box[1]+node.box[3]+gap-boxes[label][1]
            if abs(delta)<=EPS:
                continue
            proposal=_reload(_serialize(diagram),source)
            target=proposal.nodes[key].texts[0]
            target.set('y',f"{float(target.get('y'))+delta:.6f}")
            after,measured=text_audit(proposal)
            failure=None
            if set(after['overflow'])-set(audit['overflow']):
                failure='new text overflow'
            try:
                if text_conflicts(proposal)-text_conflicts(diagram):
                    failure='new text collision'
            except ValueError as exc:
                failure=str(exc)
            if failure:
                report['skipped'].append({'node':key,'reason':failure})
            else:
                report['applied'].append({'node':key,'dy':delta})
                diagram,audit,boxes=proposal,after,measured
    with destination.open('xb') as stream:
        stream.write(_serialize(diagram) if report['applied'] else source.read_bytes())
    return report
