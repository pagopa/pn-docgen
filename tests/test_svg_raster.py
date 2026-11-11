"""Offline PNG contract: canonical SVG, embedded fonts, metadata and failures."""
import hashlib
from pathlib import Path
import struct
import sys
import zlib

import pytest

from pndocgen.engine.core.models import Component, InventoryNode
from pndocgen.engine.renderers.d2_generator import D2Generator
from pndocgen.engine.renderers import d2_generator
from pndocgen.engine.renderers.svg_raster import rasterize_svg

TEMPLATES = Path(__file__).parents[1] / "pndocgen/engine/d2/templates"


def png_chunks(data):
    offset = 8
    result = {}
    while offset < len(data):
        size = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + size]
        assert zlib.crc32(kind + payload) == struct.unpack(">I", data[offset + 8 + size:offset + 12 + size])[0]
        if kind == b"tEXt":
            key, value = payload.split(b"\0", 1)
            result[key.decode()] = value.decode("latin-1")
        if kind == b"IHDR":
            result["size"] = struct.unpack(">II", payload[:8])
        offset += size + 12
    return result


@pytest.mark.parametrize("pattern", ["ecs_microservice", "lambda_microservice"])
@pytest.mark.parametrize("detail", ["simplified", "detailed"])
@pytest.mark.parametrize("theme", ["white", "pastel"])
def test_png_is_derived_from_final_svg(pattern, detail, theme, tmp_path):
    pytest.importorskip("resvg_py")
    component = Component("example", "local")
    kind, cluster = ("ecs_service", "ecs") if pattern == "ecs_microservice" else ("lambda", "lambdas")
    component.add_node(cluster, InventoryNode("test://worker", "worker", kind, "local", "local"))
    generator = D2Generator(TEMPLATES, render_theme=theme)
    d2 = generator.generate_l3(component, tmp_path / "diagram.d2", pattern=pattern, detail_level=detail)
    png = generator.render(d2, "png")
    svg = d2.with_suffix(".svg")
    assert svg.exists()
    metadata = png_chunks(png.read_bytes())
    assert metadata["Source-SVG-SHA256"] == hashlib.sha256(svg.read_bytes()).hexdigest()
    assert "CC BY-ND 2.0" in metadata["Description"]
    assert "resvg-py 0.5.0" in metadata["Software"]
    repeat = rasterize_svg(svg, tmp_path / "repeat.png")
    assert repeat.read_bytes() == png.read_bytes()
    with pytest.raises(FileExistsError):
        generator.render(d2, "png")


def test_png_backend_is_optional_for_svg(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "resvg_py", None)
    d2 = tmp_path / "diagram.d2"
    d2.write_text("a -> b")
    generator = D2Generator(TEMPLATES)
    with pytest.raises(RuntimeError, match="optional renderer"):
        generator.render(d2, "png")
    assert not d2.with_suffix(".svg").exists()
    assert generator.render(d2, "svg").exists()


def test_failed_raster_does_not_publish_svg_or_png(tmp_path, monkeypatch):
    monkeypatch.setattr(d2_generator, "raster_backend", lambda: None)
    def fail(*args):
        raise RuntimeError("raster failed")
    monkeypatch.setattr(d2_generator, "rasterize_svg", fail)
    d2 = tmp_path / "diagram.d2"
    d2.write_text("a -> b")
    with pytest.raises(RuntimeError, match="raster failed"):
        D2Generator(TEMPLATES).render(d2, "png")
    assert list(tmp_path.iterdir()) == [d2]


def test_existing_canonical_svg_is_reused_only_if_identical(tmp_path):
    pytest.importorskip("resvg_py")
    d2 = tmp_path / "diagram.d2"
    d2.write_text("a -> b")
    generator = D2Generator(TEMPLATES)
    svg = generator.render(d2)
    original = svg.read_bytes()
    assert generator.render(d2, "png").exists()
    assert svg.read_bytes() == original
    changed = tmp_path / "changed.d2"
    changed.write_text("a -> b")
    sibling = changed.with_suffix(".svg")
    sibling.write_text("existing unrelated SVG")
    with pytest.raises(FileExistsError, match="differs"):
        generator.render(changed, "png")
    assert sibling.read_text() == "existing unrelated SVG"
    assert not changed.with_suffix(".png").exists()


@pytest.mark.parametrize("body,dimensions,reason", [
    ('<text x="0" y="10">label</text>', 'width="100" height="100"', "unverified embedded font"),
    ('', 'width="10000" height="10000"', "megapixel"),
    ('<image href="external.png"/>', 'width="100" height="100"', "self-contained"),
])
def test_unsupported_input_fails_explicitly(tmp_path, body, dimensions, reason):
    pytest.importorskip("resvg_py")
    source = tmp_path / "source.svg"
    source.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" {dimensions}>{body}</svg>')
    with pytest.raises(ValueError, match=reason):
        rasterize_svg(source, tmp_path / "out.png")
    assert not (tmp_path / "out.png").exists()
