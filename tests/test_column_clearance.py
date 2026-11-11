"""Synthetic regression: repair an attached label while preserving its column."""
import base64
import re
import pytest
from test_container_titles import font_css
from pndocgen.engine.renderers.svg_geometry import Diagram
from pndocgen.engine.renderers.svg_containers import text_conflicts
from pndocgen.engine.renderers.svg_node_clearance import separate_node_labels


def sample(tmp_path, css):
    def group(name, body, cls=''):
        ident = base64.b64encode(name.encode()).decode()
        return f'<g class="{ident} {cls}"><g class="shape">{body}</g></g>'
    body = group('column', '<rect x="170" y="10" width="370" height="330"/>', 'boundary')
    for name, y in [('a', 100), ('b', 200)]:
        body += group('column.'+name, f'<image x="250" y="{y}" width="60" height="60"/>'
            f'<text x="280" y="{y+80}" class="text-bold" style="text-anchor:middle;font-size:14px">LongLongLongLabel</text>', 'aws_node')
    body += group('source', '<image x="20" y="100" width="60" height="60"/>', 'aws_node')
    body += group('(source -> column.a)[0]', '<path class="connection" d="M 80 130 L 150 130 L 150 190 L 220 190 L 220 130 L 250 130"/>')
    cls = re.search(r'd2-\d+', css)[0]
    source = tmp_path/'source.svg'
    source.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" class="{cls}" viewBox="0 0 600 380"><style>{css}</style>{body}</svg>')
    return source


def test_attached_column_moves_as_unit_and_is_idempotent(tmp_path, font_css):
    source = sample(tmp_path, font_css)
    before = Diagram.load(source)
    assert text_conflicts(before)
    target = tmp_path/'after.svg'
    report = separate_node_labels(source, target)
    after = Diagram.load(target)
    assert any(r.get('mode') == 'sibling column' for r in report['applied'])
    assert not text_conflicts(after)
    shifts = [after.nodes[k].center[0]-before.nodes[k].center[0] for k in ('column.a', 'column.b')]
    assert shifts[0] == pytest.approx(shifts[1]) and shifts[0] > 0
    assert before.viewbox == after.viewbox
    assert before.containers['column'].box == after.containers['column'].box
    assert [(e.source,e.target) for e in before.edges] == [(e.source,e.target) for e in after.edges]
    twice = tmp_path/'twice.svg'
    assert not separate_node_labels(target, twice)['applied']
    assert twice.read_bytes() == target.read_bytes()


def test_column_shift_limit_preserves_original(tmp_path, font_css):
    source = sample(tmp_path, font_css)
    target = tmp_path/'after.svg'
    assert not separate_node_labels(source, target, max_node_shift=0)['applied']
    assert target.read_bytes() == source.read_bytes()


def test_column_honors_terminal_limit(tmp_path, font_css):
    source = sample(tmp_path, font_css)
    target = tmp_path/'after.svg'
    assert not separate_node_labels(source, target, min_terminal=1000)['applied']
    assert target.read_bytes() == source.read_bytes()


@pytest.mark.parametrize('limit', [-1, float('nan'), float('inf'), True])
def test_invalid_terminal_limit_rejected(tmp_path, limit):
    with pytest.raises(ValueError):
        separate_node_labels(tmp_path/'missing.svg', tmp_path/'after.svg', min_terminal=limit)
