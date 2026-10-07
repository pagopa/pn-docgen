"""Synthetic geometry contracts for collision repair; routes stay untouched."""
import base64
import re
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest
from test_container_titles import font_css
from pndocgen.engine.renderers.svg_node_clearance import separate_node_labels
from pndocgen.engine.renderers.svg_geometry import Diagram
from pndocgen.engine.renderers.svg_containers import text_conflicts
from pndocgen.engine.renderers.svg_preferences import center_by_alternatives
from pndocgen.engine.renderers.svg_geometry import find_port_spans


def sample(tmp_path, css, effect=''):
    def group(name, content, extra=''):
        ident = base64.b64encode(name.encode()).decode()
        return f'<g class="{ident} {extra}"><g class="shape">{content}</g></g>'
    body = group('box', '<rect x="10" y="10" width="550" height="260"/>', 'boundary')
    body += group('box.a', '<image x="230" y="100" width="60" height="60"/>'
                  '<text x="260" y="180" class="text-bold" style="text-anchor:middle;font-size:14px">LongLongLongLabel</text>', 'aws_node')
    for name, x in [('b', 20), ('c', 460)]:
        body += group('box.'+name, f'<image x="{x}" y="110" width="60" height="60"/>', 'aws_node')
    body += group('(box.b -> box.c)[0]', '<path class="connection" d="M 80 140 L 210 140 L 210 210 L 400 210 L 400 140 L 460 140"/>')
    cls = re.search(r'd2-\d+', css)[0]
    path = tmp_path/'source.svg'
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" class="{cls}" viewBox="0 0 580 300" {effect}>'
                    f'<style>{css}</style>{body}</svg>')
    return path


def test_unconnected_icon_moves_with_label_without_changing_paths(tmp_path, font_css):
    source = sample(tmp_path, font_css)
    before = Diagram.load(source)
    assert text_conflicts(before)
    target = tmp_path/'after.svg'
    report = separate_node_labels(source, target)
    after = Diagram.load(target)
    assert report['applied'] and not text_conflicts(after)
    assert before.edges[0].path.element.attrib == after.edges[0].path.element.attrib
    assert after.nodes['box.a'].center[0] - before.nodes['box.a'].center[0] == pytest.approx(
        float(after.nodes['box.a'].texts[0].get('x'))-float(before.nodes['box.a'].texts[0].get('x')))
    second = tmp_path/'twice.svg'
    assert not separate_node_labels(target, second)['applied']
    assert second.read_bytes() == target.read_bytes()


@pytest.mark.parametrize('effect', ['filter="url(#f)"', 'clip-path="url(#c)"', 'transform="translate(1 0)"'])
def test_effects_preserve_source(tmp_path, font_css, effect):
    source = sample(tmp_path, font_css, effect)
    destination = tmp_path/'after.svg'
    assert not separate_node_labels(source, destination)['applied']
    assert destination.read_bytes() == source.read_bytes()


def test_zero_limits_and_existing_output(tmp_path, font_css):
    source = sample(tmp_path, font_css)
    destination = tmp_path/'after.svg'
    assert not separate_node_labels(source, destination, max_shift=0, max_node_shift=0)['applied']
    assert destination.read_bytes() == source.read_bytes()
    with pytest.raises(FileExistsError):
        separate_node_labels(source, destination)


def test_fixed_single_port_may_remain_offcenter_while_bundle_centers(tmp_path):
    # Existing public fixture: deliberately offset its single incoming port.
    source = Path(__file__).parent/'fixtures/lambda_single_unbalanced.svg'
    diagram = Diagram.load(source)
    node_id = 'lambdas.stress_lambda_single_lambda_00'
    for edge, at_source in diagram.incident(node_id):
        if not at_source:
            for command in edge.path.commands:
                for i in range(1, len(command.values), 2):
                    command.values[i] -= 3
            edge.path.write()
    local = tmp_path/'source.svg'
    diagram.tree.write(local, encoding='utf-8', xml_declaration=True)
    original = Diagram.load(local)
    target = tmp_path/'centered.svg'
    report = center_by_alternatives(local, target)
    final = Diagram.load(target)
    assert report['applied']
    assert not [s for s in find_port_spans(final) if s.node_id == node_id and abs(s.offset) > .6]
    assert [e.path.element.get('d') for e in final.edges] == [e.path.element.get('d') for e in original.edges]
