"""Dense labels sometimes require an atomic pair move within existing limits."""
import xml.etree.ElementTree as ET
import pytest

from test_explicit_alignment import fixture
from test_text_containers import font_css
from pndocgen.engine.renderers.svg_geometry import Diagram, collisions, rect_box
from pndocgen.engine.renderers.svg_labels import separate_labels

NS = '{http://www.w3.org/2000/svg}'


def crowded(tmp_path, font_css):
    source = fixture(tmp_path, [('a.x', 100, 100, 'rect'), ('b.x', 800, 200, 'rect')],
                     [('a.x', 'b.x', 'M 112 100 L 600 100 L 600 200 L 788 200'),
                      ('a.x', 'b.x', 'M 112 110 L 479 110 C 489 110 489 110 489 120 L 489 190 C 489 200 489 200 499 200 L 788 200'),
                      ('a.x', 'b.x', 'M 112 120 L 429 120 C 439 120 439 120 439 130 L 439 190 C 439 200 439 200 449 200 L 788 200')])
    tree = ET.parse(source)
    ET.SubElement(tree.getroot(), NS + 'style').text = font_css
    mask = ET.SubElement(tree.getroot(), NS + 'mask', id='labels')
    groups = [g for g in tree.getroot().iter(NS + 'g') if g.find(NS + 'path') is not None]
    for group, x, y in zip(groups, (263, 349, 393), (100, 110, 120)):
        group.find(NS + 'path').set('mask', 'url(#labels)')
        ET.SubElement(group, NS + 'text', x=str(x + 42.5), y=str(y),
                      attrib={'class': 'text-bold', 'style': 'text-anchor:middle;font-size:10px'}).text = 'relation'
        ET.SubElement(mask, NS + 'rect', x=str(x), y=str(y - 6), width='85', height='13', fill='black')
    tree.write(source)
    return source


def test_atomic_pair_separates_without_moving_paths_or_exceeding_limit(tmp_path, font_css):
    source = crowded(tmp_path, font_css)
    before = Diagram.load(source)
    assert len(collisions(before)) == 1
    output = tmp_path / 'out.svg'
    report = separate_labels(source, output, max_shift=120)
    after = Diagram.load(output)
    assert report['applied'] and not collisions(after)
    assert any(row.get('paired') for row in report['applied'])
    assert [e.path.element.get('d') for e in after.edges] == [e.path.element.get('d') for e in before.edges]
    for old, new in zip(before.edges, after.edges):
        assert abs(rect_box(old.label_mask)[0] - rect_box(new.label_mask)[0]) <= 120
    repeat = tmp_path / 'repeat.svg'
    separate_labels(output, repeat, max_shift=120)
    assert repeat.read_bytes() == output.read_bytes()


def test_pair_cannot_bypass_configured_zero_limit(tmp_path, font_css):
    source = crowded(tmp_path, font_css)
    output = tmp_path / 'out.svg'
    report = separate_labels(source, output, max_shift=0)
    assert not report['applied']
    assert output.read_bytes() == source.read_bytes()


@pytest.mark.parametrize('limit', [30, 60, 90, 120])
def test_dense_labels_respect_total_displacement_and_never_add_collisions(tmp_path, font_css, limit):
    source = crowded(tmp_path, font_css)
    before = Diagram.load(source)
    output = tmp_path / 'out.svg'
    separate_labels(source, output, max_shift=limit)
    after = Diagram.load(output)
    assert not (collisions(after) - collisions(before))
    for old, new in zip(before.edges, after.edges):
        a, b = rect_box(old.label_mask), rect_box(new.label_mask)
        assert abs(a[0] - b[0]) <= limit and abs(a[1] - b[1]) <= limit
