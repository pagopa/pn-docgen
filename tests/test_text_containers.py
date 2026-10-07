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


def test_container_fits_ragged_column_after_alignment_abstention(tmp_path, font_css):
    source, contracts = synthetic(tmp_path, font_css)
    tree = ET.parse(source)
    diagram = Diagram.from_tree(tree, source)
    diagram.nodes['rules.n1'].translate(18, 0)
    tree.write(source)
    before = Diagram.load(source)
    out = tmp_path/'ragged-fit.svg'
    result = fit_containers(source, out, contracts)
    assert result['applied'] and not result['after']['overflow']
    after = Diagram.load(out)
    assert {k: ET.tostring(n.group) for k,n in before.nodes.items()} == {
        k: ET.tostring(n.group) for k,n in after.nodes.items()}


def test_pipeline_fits_text_before_safe_alignment(tmp_path, font_css):
    from pndocgen.engine.renderers.svg_layout import normalize_layout
    source, contracts = synthetic(tmp_path, font_css)
    tree = ET.parse(source)
    diagram = Diagram.from_tree(tree, source)
    diagram.nodes['rules.n1'].translate(18, 0)
    tree.write(source)
    out = tmp_path/'normalized.svg'
    normalize_layout(source, out, contracts, NormalizationConfig())
    after = Diagram.load(out)
    assert not text_audit(after)[0]['overflow']
    assert len({n.center[0] for n in after.nodes.values()}) == 1
    repeat = tmp_path/'normalized-repeat.svg'
    normalize_layout(out, repeat, contracts, NormalizationConfig())
    assert out.read_bytes() == repeat.read_bytes()


def test_final_fitting_reclaims_temporary_width_without_shrinking_original(tmp_path, font_css):
    from pndocgen.engine.renderers.svg_layout import normalize_layout
    source, contracts = synthetic(tmp_path, font_css)
    tree = ET.parse(source)
    diagram = Diagram.from_tree(tree, source)
    for node in diagram.nodes.values():
        node.texts[0].text = 'Short'
    diagram.nodes['rules.n1'].translate(200, 0)
    tree.write(source)
    out = tmp_path/'compact.svg'
    report = normalize_layout(source, out, contracts, NormalizationConfig())
    assert report['initial_text_containers']['applied'][0]['after'][2] > 280
    after = Diagram.load(out)
    assert after.containers['rules'].box[2] == 280
    assert len({n.center[0] for n in after.nodes.values()}) == 1
    assert not text_audit(after)[0]['overflow']
    repeat = tmp_path/'compact-repeat.svg'
    normalize_layout(out, repeat, contracts, NormalizationConfig())
    assert out.read_bytes() == repeat.read_bytes()


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


@pytest.mark.parametrize('with_label', [False, True])
def test_initial_alignment_checks_measured_text_before_publication(tmp_path, font_css, with_label):
    from test_explicit_alignment import fixture
    from pndocgen.engine.renderers.svg_normalizer import normalize_svg
    from pndocgen.engine.renderers.svg_layout import normalize_layout
    from pndocgen.engine.renderers.svg_containers import text_conflicts
    source = fixture(tmp_path, [('group.a', 100, 100, 'rect'),
        ('group.b', 100, 200, 'rect'), ('group.c', 80, 300, 'rect')],
        [('outside.a', 'outside.b', 'M 105 325 L 105 350')])
    tree = ET.parse(source)
    ET.SubElement(tree.getroot(), NS+'style').text = font_css
    if with_label:
        node = Diagram.from_tree(tree, source).nodes['group.c']
        ET.SubElement(node.group, NS+'text', {'x':'80', 'y':'340',
            'class':'text-bold', 'style':'text-anchor:middle;font-size:10px'}).text = 'Short'
    tree.write(source)
    assert not text_conflicts(Diagram.load(source))
    normalized = tmp_path/'normalized.svg'
    report = normalize_svg(source, normalized)
    assert bool(report.aligned) == (not with_label), report
    assert not text_conflicts(Diagram.load(normalized))
    if with_label:
        assert any('new text collision' in reason for reason in report.rolled_back)
        assert Diagram.load(normalized).nodes['group.c'].center == (80, 300)
    final = tmp_path/'final.svg'
    normalize_layout(normalized, final, {'group':{'layout':'column','columns':1}}, NormalizationConfig())
    assert not text_conflicts(Diagram.load(final))


@pytest.mark.parametrize('with_label', [False, True])
def test_initial_port_centering_checks_measured_text(tmp_path, font_css, with_label):
    from test_svg_normalizer import _collision_fixture
    from pndocgen.engine.renderers.svg_normalizer import normalize_svg
    from pndocgen.engine.renderers.svg_containers import text_conflicts
    source = _collision_fixture(tmp_path)
    tree = ET.parse(source)
    root = tree.getroot()
    diagram = Diagram.from_tree(tree, source)
    root.remove(diagram.nodes['obstacle'].group)
    ET.SubElement(root, NS+'style').text = font_css
    if with_label:
        ET.SubElement(diagram.nodes['dst1'].group, NS+'text', {'x':'80', 'y':'32',
            'class':'text-bold', 'style':'text-anchor:middle;font-size:10px'}).text = 'Short'
    tree.write(source)
    assert not text_conflicts(Diagram.load(source))
    out = tmp_path/'normalized.svg'
    report = normalize_svg(source, out)
    assert bool(report.centered) == (not with_label), report
    assert not text_conflicts(Diagram.load(out))
    if with_label:
        assert any('new text collision' in reason for reason in report.rolled_back)
        assert [e.path.element.get('d') for e in Diagram.load(out).edges] == [
            e.path.element.get('d') for e in Diagram.load(source).edges]


@pytest.mark.parametrize('effect', ['filter', 'clip-path'])
def test_initial_normalization_preserves_unsupported_effects(tmp_path, effect):
    from test_svg_normalizer import _collision_fixture
    from pndocgen.engine.renderers.svg_normalizer import normalize_svg
    source = _collision_fixture(tmp_path)
    tree = ET.parse(source)
    tree.getroot().set(effect, 'url(#effect)')
    tree.write(source)
    before = source.read_bytes()
    out = tmp_path/'normalized.svg'
    report = normalize_svg(source, out)
    assert report.skipped
    assert out.read_bytes() == before


@pytest.mark.parametrize('effect', ['clip-path', 'filter'])
@pytest.mark.parametrize('side', ['before', 'after'])
def test_shared_text_gate_rejects_unmodeled_effects(tmp_path, font_css, effect, side):
    from pndocgen.engine.renderers.svg_containers import measured_text_gate
    source, _ = synthetic(tmp_path, font_css)
    before, after = Diagram.load(source), Diagram.load(source)
    diagram = before if side == 'before' else after
    diagram.nodes['rules.n0'].group.set(effect, 'url(#effect)')
    assert 'unsupported SVG effects' in (measured_text_gate(before, after) or '')


@pytest.mark.parametrize('pipeline_mode', [False, True])
def test_container_fitting_rejects_new_incident_edge_label_collision(tmp_path, font_css, pipeline_mode):
    from pndocgen.engine.renderers.svg_containers import text_conflicts
    source, contracts = synthetic(tmp_path, font_css)
    tree = ET.parse(source)
    root = tree.getroot()
    encoded = base64.b64encode(b'(rules.n0 -> sink)[0]').decode()
    edge = ET.SubElement(root, NS+'g', {'class': encoded})
    ET.SubElement(edge, NS+'path', {'d': 'M 192 135 L 450 135'})
    label = ET.SubElement(edge, NS+'text', {'x': '160', 'y': '85',
        'class': 'text-bold', 'style': 'text-anchor:middle;font-size:10px'})
    label.text = 'reads'
    tree.write(source)
    before = source.read_bytes()
    out = tmp_path/'fitted.svg'
    if pipeline_mode:
        from pndocgen.engine.renderers.svg_layout import normalize_layout
        settings = NormalizationConfig(separate_container_titles=False,
            prefer_free_nodes=False, center_singletons=False, center_grouped_nodes=False,
            separate_edge_labels=False, align_node_labels=False, port_capacity_classes=(), max_shift=0)
        report = normalize_layout(source, out, contracts, settings)['text_containers']
    else:
        report = fit_containers(source, out, contracts)
    assert not report['applied'], report
    assert any('new text collision' in item['reason'] for item in report['skipped'])
    assert out.read_bytes() == before
    assert text_conflicts(Diagram.load(out)) == text_conflicts(Diagram.load(source))


@pytest.mark.parametrize('effect', ['clip-path', 'filter'])
def test_alignment_abstains_when_effect_bounds_are_unknown(tmp_path, effect):
    from test_explicit_alignment import fixture
    from pndocgen.engine.renderers.svg_alignment import normalize, Rule
    from pndocgen.engine.renderers.svg_preferences import _gate
    source = fixture(tmp_path, [('group.a', 100, 100, 'rect'), ('group.b', 130, 200, 'rect')])
    control = tmp_path/'control.svg'
    assert normalize(source, control, {'group': Rule('column')})['applied']
    tree = ET.parse(source)
    tree.getroot().set(effect, 'url(#effect)')
    tree.write(source)
    before = source.read_bytes()
    out = tmp_path/'guarded.svg'
    result = normalize(source, out, {'group': Rule('column')})
    assert not result['applied']
    assert any('unsupported SVG effects' in item['reason'] for item in result['skipped'])
    assert out.read_bytes() == before
    assert 'unsupported SVG effects' in _gate(Diagram.load(source), Diagram.load(source))


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


@pytest.mark.parametrize('pipeline_mode', [False, True])
def test_capacity_honors_configured_terminal_floor(tmp_path, font_css, pipeline_mode):
    from pndocgen.engine.renderers.svg_capacity import fit_ports
    from pndocgen.engine.renderers.svg_layout import normalize_layout
    source = capacity_source(tmp_path, font_css)
    contracts = {'rules': {'layout': 'column', 'columns': 1}}
    # Existing terminals are 208px; icon growth shortens them by 9px.
    # 205 therefore permits the input, but must reject that transformation.
    for floor in (10, 205):
        out = tmp_path / f'floor-{floor}.svg'
        if pipeline_mode:
            settings = NormalizationConfig(
                min_terminal=floor, fit_text_containers=False,
                prefer_free_nodes=False, center_singletons=False,
                center_grouped_nodes=False, separate_edge_labels=False,
                align_node_labels=False, separate_container_titles=False,
                max_shift=0)
            result = normalize_layout(source, out, contracts, settings)['port_capacity']
        else:
            result = fit_ports(source, out, contracts=contracts, min_terminal=floor)
        assert bool(result['applied']) == (floor == 10), result
        if floor == 205:
            assert any('min_terminal' in reason for _, reason in result['skipped'])
            assert Diagram.load(out).nodes['rules.n0'].box == Diagram.load(source).nodes['rules.n0'].box
            assert [e.path.element.get('d') for e in Diagram.load(out).edges] == [
                e.path.element.get('d') for e in Diagram.load(source).edges]


def test_centered_overfull_bundle_is_resized(tmp_path, font_css):
    from pndocgen.engine.renderers.svg_capacity import fit_ports
    source = capacity_source(tmp_path, font_css)
    tree = ET.parse(source)
    diagram = Diagram.from_tree(tree, source)
    diagram.nodes['rules.n0'].translate(0, 8)
    tree.write(source)
    out = tmp_path/'centered.svg'
    result = fit_ports(source, out, contracts={'rules': {'layout': 'column'}})
    assert result['applied'], result
    assert result['applied'][0]['shift'] == 0
    assert Diagram.load(out).nodes['rules.n0'].box[2:] == (78, 78)
    repeat = tmp_path/'centered-repeat.svg'
    assert not fit_ports(out, repeat, contracts={'rules': {'layout': 'column'}})['applied']
    assert out.read_bytes() == repeat.read_bytes()


def test_measured_node_label_overlaps_edge_label_without_route_collision(tmp_path, font_css):
    from pndocgen.engine.renderers.svg_containers import text_conflicts, measured_text_gate
    source, _ = synthetic(tmp_path, font_css)
    before = Diagram.load(source)
    tree = ET.parse(source)
    diagram = Diagram.from_tree(tree, source)
    group = ET.SubElement(tree.getroot(), NS+'g', {
        'class': base64.b64encode(b'(rules.n0 -> rules.n1)[0]').decode()})
    ET.SubElement(group, NS+'path', d='M 300 100 L 400 100')
    text = diagram.nodes['rules.n0'].texts[0]
    ET.SubElement(group, NS+'text', dict(text.attrib)).text = 'Short'
    after = Diagram.from_tree(tree, source)
    assert any(issue[0] == 'text-edge-label' for issue in text_conflicts(after))
    assert measured_text_gate(before, after) == 'new text collision'


def test_alignment_and_preferences_reject_unmeasured_text(tmp_path):
    from test_explicit_alignment import fixture
    from pndocgen.engine.renderers.svg_alignment import normalize, Rule
    from pndocgen.engine.renderers.svg_preferences import _gate
    source = fixture(tmp_path, [('group.a', 100, 100, 'rect'), ('group.b', 118, 230, 'rect')])
    tree = ET.parse(source)
    diagram = Diagram.from_tree(tree, source)
    ET.SubElement(diagram.nodes['group.a'].group, NS+'text', x='100', y='130').text = 'Unknown'
    tree.write(source)
    before = Diagram.load(source)
    assert 'unmeasured' in _gate(before, before)
    out = tmp_path/'unsafe.svg'
    result = normalize(source, out, {'group': Rule('column')})
    assert not result['applied']
    assert 'unmeasured' in result['skipped'][0]['reason']
    assert out.read_bytes() == source.read_bytes()


@pytest.mark.parametrize('layout', ['row', 'grid'])
def test_alignment_rolls_back_new_measured_label_overlap(tmp_path, font_css, layout):
    from test_explicit_alignment import fixture
    from pndocgen.engine.renderers.svg_alignment import normalize, Rule
    from pndocgen.engine.renderers.svg_containers import text_conflicts
    source = fixture(tmp_path, [('group.a', 100, 100, 'rect'), ('group.b', 170, 200, 'rect')])
    tree = ET.parse(source)
    ET.SubElement(tree.getroot(), NS+'style').text = font_css
    diagram = Diagram.from_tree(tree, source)
    for node in diagram.nodes.values():
        ET.SubElement(node.group, NS+'text', x=str(node.center[0]), y=str(node.center[1]+30),
                      attrib={'class': 'text-bold', 'style': 'text-anchor:middle;font-size:10px'}).text = 'LongLabelABCDEFGHIJKLMNOPQRSTUVWXYZ'
    tree.write(source)
    assert not text_conflicts(Diagram.load(source))
    out = tmp_path/'guarded.svg'
    report = normalize(source, out, {'group': Rule(layout, columns=2)})
    assert not report['applied'], report
    assert 'text collision' in report['skipped'][0]['reason']
    assert out.read_bytes() == source.read_bytes()


def test_preference_gate_rejects_label_crossing_unrelated_route(tmp_path, font_css):
    from test_explicit_alignment import fixture
    from pndocgen.engine.renderers.svg_preferences import _gate
    from pndocgen.engine.renderers.svg_normalizer import _reload, _serialize
    source = fixture(tmp_path, [('group.a', 100, 100, 'rect')],
                     [('else.a', 'else.b', 'M 50 160 L 150 160')])
    tree = ET.parse(source)
    ET.SubElement(tree.getroot(), NS+'style').text = font_css
    node = Diagram.from_tree(tree, source).nodes['group.a']
    ET.SubElement(node.group, NS+'text', x='100', y='130',
                  attrib={'class': 'text-bold', 'style': 'text-anchor:middle;font-size:10px'}).text = 'Short'
    tree.write(source)
    before = Diagram.load(source)
    after = _reload(_serialize(before), source)
    after.nodes['group.a'].translate(0, 30)
    assert _gate(before, after) == 'new text collision'


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
