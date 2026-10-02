"""Local-only checks for AWS icon attribution in portable artifacts."""

import hashlib
import runpy
import xml.etree.ElementTree as ET
from pathlib import Path

from pndocgen.engine.renderers.assets import (
    ASSET_NOTICE_NAME,
    ICON_NOTICE,
    SVG_NOTICE_ID,
    annotate_svg_icons,
    portable_icons,
)


def test_portable_icons_include_notice_without_changing_icon_bytes(tmp_path: Path) -> None:
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"unchanged PNG bytes")
    output = tmp_path / "diagram.d2"
    content = f"icon: {icon}\n"
    converted = portable_icons(content, output, [icon])
    assert converted.startswith("icon: assets/")
    assets = output.parent / "assets"
    assert (assets / ASSET_NOTICE_NAME).read_bytes() == ICON_NOTICE.read_bytes()
    png = next(assets.glob("*.png"))
    assert png.read_bytes() == icon.read_bytes()
    assert portable_icons(content, output, [icon]) == converted


def test_svg_notice_is_conditional_and_idempotent(tmp_path: Path) -> None:
    svg = tmp_path / "diagram.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 60 60">'
        '<g class="aws_node"><image x="0" y="0" width="60" height="60"/></g>'
        '</svg>'
    )
    assert annotate_svg_icons(svg)
    first = svg.read_bytes()
    assert not annotate_svg_icons(svg)
    assert svg.read_bytes() == first
    root = ET.parse(svg).getroot()
    metadata = root.find("{http://www.w3.org/2000/svg}metadata")
    assert metadata is not None and metadata.get("id") == SVG_NOTICE_ID
    assert "https://creativecommons.org/licenses/by-nd/2.0/" in (metadata.text or "")
    image = root.find(".//{http://www.w3.org/2000/svg}image")
    assert image is not None and image.get("width") == "60"

    without_icon = tmp_path / "plain.svg"
    without_icon.write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
    original = without_icon.read_bytes()
    assert not annotate_svg_icons(without_icon)
    assert without_icon.read_bytes() == original


def test_frozen_icon_hashes_match_local_pngs() -> None:
    script = ICON_NOTICE.parents[5] / "scripts/download_aws_icons.py"
    definitions = runpy.run_path(str(script))
    names = definitions["ICONS"]
    hashes = definitions["EXPECTED_SHA256"]
    assert set(names) == set(hashes)
    assert len(names) == 21
    for name in names:
        png = ICON_NOTICE.parent / f"{name}.png"
        assert hashlib.sha256(png.read_bytes()).hexdigest() == hashes[name]


def test_notice_includes_permission_and_pinned_license_sources() -> None:
    notice = ICON_NOTICE.read_text()
    assert "https://aws.amazon.com/architecture/icons/" in notice
    assert "e26e2c05daf8b6bc4c764669fc2be04c314ccb8c/README.md#license-summary" in notice
    assert "e26e2c05daf8b6bc4c764669fc2be04c314ccb8c/LICENSE" in notice
    assert "https://creativecommons.org/licenses/by-nd/2.0/legalcode.en" in notice
    assert "It does not add\nvisible credits or change the diagram layout" in notice
