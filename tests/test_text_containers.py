"""Public synthetic text geometry fixtures; no private graphs or AWS clients."""
import base64
import re
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import pytest

from pndocgen.engine.renderers.svg_geometry import Diagram, NS
from pndocgen.engine.renderers.svg_text import TextMetrics, UnsupportedText
from pndocgen.engine.renderers.svg_containers import fit_containers, text_audit, contains
from pndocgen.engine.core.normalization_config import NormalizationConfig


@pytest.fixture(scope='module')
def font_css(tmp_path_factory):
    folder = tmp_path_factory.mktemp('embedded-fonts')
    source = folder / 'font.d2'
    source.write_text('a: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" {style.bold: true}\n')
    result = subprocess.run(['d2', '--layout=elk', str(source), str(folder/'font.svg')], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    root = ET.parse(folder/'font.svg').getroot()
    css = '\n'.join(e.text or '' for e in root.iter(NS+'style') if '@font-face' in (e.text or ''))
    assert css
    return css


def synthetic(tmp_path, font_css, *, nested=False, endpoint=False, neighbor=False):
    def group(name, body, cls):
        return f'<g class="{base64.b64encode(name.encode()).decode()} {cls}"><g class="shape">{body}</g></g>'
    parent = 'outer.' if nested else ''
    key = parent+'rules'
    text = lambda x,y,value: f'<text x="{x}" y="{y}" class="text-bold" style="text-anchor:middle;font-size:10px">{value}</text>'
    parts = [group(key, '<rect x="100" y="60" width="280" height="260"/>'+text(240,85,'Rules'), 'boundary')]
    if nested:
        parts.append(group('outer', '<rect x="80" y="40" width="320" height="300"/>', 'boundary'))
    for i,name in enumerate(['Short', 'LongRuleNameWithDeterministicSuffixABCDEFGHIJKLMNOPQRSTUVWXYZ']):
        y = 105+i*100
        parts.append(group(f'{key}.n{i}', f'<image x="130" y="{y}" width="60" height="60"/>'+text(160,y+76,name), 'aws_node'))
    if endpoint:
        parts.append(group(f'({key} -> storage)[0]', '<path class="connection" d="M 380 150 L 450 150"/>', ''))
    if neighbor:
        parts.append(group('neighbor', '<rect x="0" y="60" width="90" height="260"/>', 'boundary'))
    path = tmp_path/'source.svg'
    css_class = re.search(r'd2-\d+', font_css)[0]
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" class="{css_class}" viewBox="0 0 500 400" width="500" height="400"><style>{font_css}</style>'+''.join(parts)+'</svg>')
    return path, {key: {'layout':'column', 'columns':1}}


@pytest.mark.parametrize('nested', [False, True])
def test_fit_long_labels_and_center_column_without_moving_contents(tmp_path, font_css, nested):
    source, contracts = synthetic(tmp_path, font_css, nested=nested)
    original = source.read_bytes()
    before = Diagram.load(source)
    out = tmp_path/'fit.svg'
    result = fit_containers(source, out, contracts)
    after = Diagram.load(out)
    assert result['before']['overflow'] and not result['after']['overflow']
    assert not result['after']['unmeasured']
    assert {k:ET.tostring(n.group) for k,n in before.nodes.items()} == {k:ET.tostring(n.group) for k,n in after.nodes.items()}
    key = next(iter(contracts))
    assert after.containers[key].center[0] == pytest.approx(160)
    if nested:
        assert contains(after.containers['outer'].box, after.containers[key].box)
    second = tmp_path/'second.svg'
    fit_containers(out, second, contracts)
    assert out.read_bytes() == second.read_bytes()
    assert source.read_bytes() == original


@pytest.mark.parametrize('reason', ['endpoint', 'neighbor', 'growth', 'transform', 'grid'])
def test_unsafe_fitting_abstains_without_claiming_overflow_fixed(tmp_path, font_css, reason):
    source, contracts = synthetic(tmp_path, font_css, endpoint=reason=='endpoint', neighbor=reason=='neighbor')
    if reason == 'transform':
        tree = ET.parse(source)
        next(tree.getroot().iter(NS+'image')).set('transform', 'translate(0 1)')
        tree.write(source)
    if reason == 'grid':
        contracts[next(iter(contracts))]['layout'] = 'grid'
    out = tmp_path/'fit.svg'
    result = fit_containers(source, out, contracts, max_growth=0 if reason=='growth' else 240)
    assert not result['applied']
    assert result['after']['overflow']
    assert out.read_bytes() == source.read_bytes()


def test_unknown_font_is_explicit_not_zero_width(tmp_path, font_css):
    source, contracts = synthetic(tmp_path, font_css)
    tree = ET.parse(source)
    next(tree.getroot().iter(NS+'style')).text = ''
    tree.write(source)
    out = tmp_path/'fit.svg'
    result = fit_containers(source, out, contracts)
    assert result['after']['unmeasured'] and not result['applied']


def test_shaping_and_tspan_are_not_guessed(tmp_path, font_css):
    source, _ = synthetic(tmp_path, font_css)
    diagram = Diagram.load(source)
    metrics = TextMetrics(diagram.tree.getroot())
    text = next(diagram.tree.getroot().iter(NS+'text'))
    text.text = 'a\u0301'
    with pytest.raises(UnsupportedText, match='shaping'):
        metrics.box(text)
    text.text = 'Rules'
    ET.SubElement(text, NS+'tspan').text = 'more'
    with pytest.raises(UnsupportedText):
        metrics.box(text)


@pytest.mark.parametrize('mapping', [{'fit_text_containers': 'yes'}, {'text_container_margin': -1},
                                   {'max_container_growth': float('nan')}])
def test_fitting_config_validated(mapping):
    with pytest.raises(ValueError):
        NormalizationConfig.from_mapping(mapping)


def capacity_source(tmp_path,font_css):
    source,_ = synthetic(tmp_path,font_css)
    tree = ET.parse(source)
    root = tree.getroot()
    diagram = Diagram.from_tree(tree,source)
    node = diagram.nodes['rules.n0']
    diagram.nodes['rules.n1'].translate(0,15)
    node.group.set('class',node.group.get('class').replace('aws_node','ecs_hero'))
    for i,y in enumerate((106,143,180)):
        eid = base64.b64encode(f'(rules.n0 -> sink.n{i})[0]'.encode()).decode()
        group = ET.SubElement(root,NS+'g',{'class':eid})
        ET.SubElement(group,NS+'path',{'d':f'M 192 {y} L 400 {y}'})
        sink = ET.SubElement(root,NS+'g',{'class':base64.b64encode(f'sink.n{i}'.encode()).decode()+' aws_node'})
        shape = ET.SubElement(sink,NS+'g',{'class':'shape'})
        ET.SubElement(shape,NS+'rect',{'x':'400','y':str(y-5),'width':'10','height':'10'})
    tree.write(source)
    return source


def test_capacity_preserves_far_endpoints_and_square_icon(tmp_path,font_css):
    from pndocgen.engine.renderers.svg_capacity import fit_ports
    from pndocgen.engine.renderers.svg_geometry import find_port_spans
    source = capacity_source(tmp_path,font_css)
    before = Diagram.load(source)
    out = tmp_path/'capacity.svg'
    result = fit_ports(source,out,contracts={'rules':{'layout':'column'}})
    assert result['applied'],result
    after = Diagram.load(out)
    assert after.nodes['rules.n0'].box[2:] == (78,78)
    assert [e.end for e in before.edges] == [e.end for e in after.edges]
    assert all(abs(s.offset)<.6 for s in find_port_spans(after))
    assert after.nodes['rules.n1'].box == before.nodes['rules.n1'].box
    second = tmp_path/'second.svg'
    fit_ports(out,second,contracts={'rules':{'layout':'column'}})
    assert second.read_bytes() == out.read_bytes()


@pytest.mark.parametrize('option',[{'max_growth':0},{'max_shift':0},{'allowed_classes':()}])
def test_capacity_thresholds_preserve_original(tmp_path,font_css,option):
    from pndocgen.engine.renderers.svg_capacity import fit_ports
    source = capacity_source(tmp_path,font_css)
    out = tmp_path/'capacity.svg'
    result=fit_ports(source,out,contracts={'rules':{'layout':'column'}},**option)
    assert not result['applied']
    assert source.read_bytes()==out.read_bytes()


def test_node_labels_normalize_gap_without_moving_icons(tmp_path,font_css):
    from pndocgen.engine.renderers.svg_node_labels import align_node_labels
    source,_=synthetic(tmp_path,font_css)
    tree=ET.parse(source)
    diagram=Diagram.from_tree(tree,source)
    label=diagram.nodes['rules.n1'].texts[0]
    label.set('y',str(float(label.get('y'))+25))
    tree.write(source)
    before=Diagram.load(source)
    target=tmp_path/'labels.svg'
    report=align_node_labels(source,target)
    assert report['applied'] and not report['skipped']
    after=Diagram.load(target)
    _,boxes=text_audit(after)
    for key,node in after.nodes.items():
        assert node.box==before.nodes[key].box
        assert boxes[node.texts[0]][1]-node.box[1]-node.box[3] == pytest.approx(4,abs=.6)
        assert node.texts[0].text==before.nodes[key].texts[0].text
    second=tmp_path/'second.svg'
    align_node_labels(target,second)
    assert second.read_bytes()==target.read_bytes()
