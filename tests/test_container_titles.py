"""Synthetic safety tests for the title-only prototype; no AWS or browser."""
import base64
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

import pytest

from pndocgen.engine.renderers.svg_titles import separate_container_titles as repair
from pndocgen.engine.renderers.svg_geometry import Diagram
from pndocgen.engine.renderers.svg_containers import text_conflicts
from pndocgen.engine.renderers.svg_geometry import NS
from pndocgen.engine.core.normalization_config import NormalizationConfig


@pytest.fixture(scope='module')
def font_css(tmp_path_factory):
    folder = tmp_path_factory.mktemp('title-font')
    source = folder / 'font.d2'
    source.write_text('a: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789" {style.bold: true}\n')
    subprocess.run(['d2', '--layout=elk', str(source), str(folder / 'font.svg')],
                   check=True, capture_output=True, timeout=60)
    root = ET.parse(folder / 'font.svg').getroot()
    return '\n'.join(e.text or '' for e in root.iter(NS + 'style')
                     if '@font-face' in (e.text or ''))


def fixture(tmp_path, css, *, blocked=False, transform=False):
    def group(name, contents, cls=''):
        encoded = base64.b64encode(name.encode()).decode()
        return f'<g class="{encoded} {cls}"><g class="shape">{contents}</g></g>'
    title = '<text x="220" y="50" class="text-bold" style="text-anchor:middle;font-size:14px">Title</text>'
    items = [group('box', '<rect x="20" y="20" width="400" height="200"/>' + title, 'boundary')]
    for name, x in [('a', 60), ('b', 300)]:
        items.append(group('box.' + name, f'<image x="{x}" y="100" width="60" height="60"/>', 'aws_node'))
    route = 'M 20 45 L 420 45' if blocked else 'M 220 120 L 220 30'
    items.append(group('(box.a -> box.b)[0]', f'<path class="connection" d="{route}"/>'))
    effect = ' transform="translate(1 0)"' if transform else ''
    css_class = re.search(r'd2-\d+', css)[0]
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" class="{css_class}" '
           f'viewBox="0 0 450 250"{effect}><style>{css}</style>' + ''.join(items) + '</svg>')
    path = tmp_path / 'source.svg'
    path.write_text(svg)
    return path


def test_title_moves_only_when_needed_without_changing_any_other_xml(tmp_path, font_css):
    source = fixture(tmp_path, font_css)
    original = source.read_bytes()
    before = Diagram.load(source)
    assert text_conflicts(before)
    target = tmp_path / 'fixed.svg'
    report = repair(source, target)
    after = Diagram.load(target)
    assert len(report['applied']) == 1
    assert abs(report['applied'][0]['dx']) <= 240
    assert not text_conflicts(after)
    # Restore only the intentionally moved x attribute, then compare every
    # XML element/attribute/text, including icons and edge labels/paths.
    after.containers['box'].texts[0].set('x', before.containers['box'].texts[0].get('x'))
    assert ET.tostring(before.tree.getroot()) == ET.tostring(after.tree.getroot())
    second = tmp_path / 'twice.svg'
    assert not repair(target, second)['applied']
    assert target.read_bytes() == second.read_bytes()
    assert source.read_bytes() == original


@pytest.mark.parametrize('mode', ['zero', 'blocked', 'transform', 'unknown'])
def test_unsafe_or_unsupported_placement_preserves_original(tmp_path, font_css, mode):
    source = fixture(tmp_path, font_css, blocked=mode == 'blocked', transform=mode == 'transform')
    if mode == 'unknown':
        tree = ET.parse(source)
        next(tree.getroot().iter(NS + 'style')).text = ''
        tree.write(source)
    target = tmp_path / 'unchanged.svg'
    report = repair(source, target, max_shift=0 if mode == 'zero' else 240)
    assert not report['applied']
    assert report['skipped']
    assert source.read_bytes() == target.read_bytes()


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), True])
def test_limits_are_validated(tmp_path, font_css, value):
    source = fixture(tmp_path, font_css)
    with pytest.raises(ValueError):
        repair(source, tmp_path / 'invalid.svg', max_shift=value)


def test_existing_file_is_never_overwritten(tmp_path, font_css):
    source = fixture(tmp_path, font_css)
    with pytest.raises(FileExistsError):
        repair(source, source)


@pytest.mark.parametrize('mapping', [
    {'separate_container_titles': 'true'}, {'max_title_shift': -1},
    {'max_title_shift': float('inf')},
])
def test_title_configuration_rejects_invalid_values(mapping):
    with pytest.raises(ValueError):
        NormalizationConfig.from_mapping(mapping)
