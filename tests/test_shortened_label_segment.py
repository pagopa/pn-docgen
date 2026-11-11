"""Labels on a shortened adjacent segment retain their geometric attachment."""
import xml.etree.ElementTree as ET

import pytest

from pndocgen.engine.renderers.svg_geometry import Edge, EdgePath, parse_path, label_segment
from pndocgen.engine.renderers.svg_normalizer import _shift_terminal_segment


@pytest.mark.parametrize('at_source', [False, True])
@pytest.mark.parametrize('axis', ['x', 'y'])
@pytest.mark.parametrize('near_cut', [False, True])
def test_label_follows_only_when_shortening_would_detach_it(at_source, axis, near_cut):
    path = ('M 100 10 L 40 10 S 30 10 30 20 L 30 90 S 30 100 20 100 L 0 100'
            if at_source else 'M 0 100 L 20 100 S 30 100 30 90 L 30 20 S 30 10 40 10 L 100 10')
    commands = parse_path(path)
    x, y, w, h = 20, (20 if near_cut else 50), 20, 10
    if axis == 'x':
        for command in commands:
            command.values = [value for a, b in zip(command.values[::2], command.values[1::2])
                              for value in (b, a)]
        x, y, w, h = y, x, h, w
    element = ET.Element('path')
    mask = ET.Element('rect', x=str(x), y=str(y), width=str(w), height=str(h))
    label = ET.Element('text', x=str(x+w/2), y=str(y+h/2))
    edge = Edge('edge', 'source', 'target', EdgePath(element, commands), label, mask)
    old = label_segment(edge)
    original = float(label.get(axis))
    assert _shift_terminal_segment(edge, at_source, 10, axis) is None
    assert label_segment(edge)[1] <= old[1] + 0.001
    expected = 10 if near_cut else 0
    assert float(label.get(axis)) == original + expected
    assert float(mask.get(axis)) == (x if axis == 'x' else y) + expected
    assert len(edge.path.commands) == len(commands)
