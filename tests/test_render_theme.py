from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from pndocgen.engine.core.config import AppConfig, RenderConfig
from pndocgen.engine.core.models import Component, ComponentGraph, Edge, InventoryNode
from pndocgen.engine.renderers.d2_generator import D2Generator
from pndocgen.services.pipeline import _resolve_render_theme


TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "pndocgen" / "engine" / "d2" / "templates"
FILL_RE = re.compile(r'style\.fill: "(?:transparent|#[0-9A-Fa-f]{6})"')


def _write_config(path: Path, theme: str | None = None) -> Path:
    content = "{}\n" if theme is None else f"render:\n  theme: {theme}\n"
    path.write_text(content)
    return path


def _node(name: str, resource_type: str) -> InventoryNode:
    return InventoryNode(
        id=f"test://{resource_type}/{name}",
        name=name,
        resource_type=resource_type,
        account="test",
        region="eu-test-1",
    )


def _ecs_fixture() -> tuple[Component, list[Edge]]:
    component = Component(name="pn-example", account="test")
    component.add_node("apigw", _node("pn-example-api", "apigw"))
    component.add_node("queues", _node("pn-example-input", "sqs"))
    component.add_node("ecs", _node("pn-example", "ecs_service"))
    component.add_node("dynamodb", _node("pn-example-table", "dynamodb"))
    component.canonicalize()
    edges = [
        Edge("pn-example-api", "pn-example", "apigw_integration"),
        Edge("pn-example-input", "pn-example", "sqs_consumer"),
        Edge("pn-example", "pn-example-table", "storage_access"),
    ]
    return component, edges


def _normalize_fills(d2_text: str) -> str:
    return FILL_RE.sub('style.fill: "<theme-fill>"', d2_text)


def _geometry_signature(svg_path: Path) -> tuple[str, tuple[tuple[object, ...], ...]]:
    root = ET.parse(svg_path).getroot()
    geometry_attributes = (
        "x", "y", "width", "height", "rx", "ry", "x1", "y1", "x2", "y2",
        "cx", "cy", "r", "d", "points", "transform",
    )
    signature: list[tuple[object, ...]] = []
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name not in {
            "rect", "image", "path", "polygon", "polyline", "circle", "ellipse",
            "line", "text",
        }:
            continue
        signature.append(
            (
                local_name,
                *(element.get(attribute) for attribute in geometry_attributes),
                (element.text or "").strip() if local_name == "text" else "",
            )
        )
    return root.get("viewBox", ""), tuple(signature)


def test_render_theme_defaults_to_white() -> None:
    assert RenderConfig().theme == "white"


def test_render_theme_is_loaded_and_normalized_from_yaml(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path / "pndocgen.yaml", " PaStEl ")

    assert AppConfig.from_yaml(config_path).render.theme == "pastel"


def test_invalid_render_theme_falls_back_to_white(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_path = _write_config(tmp_path / "pndocgen.yaml", "neon")

    config = AppConfig.from_yaml(config_path)

    assert config.render.theme == "white"
    assert "Unknown render.theme 'neon'" in caplog.text


def test_render_theme_precedence_is_cli_then_yaml_then_default(tmp_path: Path) -> None:
    pastel_config = _write_config(tmp_path / "pastel.yaml", "pastel")
    empty_config = _write_config(tmp_path / "empty.yaml")

    args = SimpleNamespace(config=str(pastel_config), theme="white")
    assert _resolve_render_theme(args) == "white"
    assert _resolve_render_theme(SimpleNamespace(config=str(pastel_config), theme=None)) == "pastel"
    assert _resolve_render_theme(SimpleNamespace(config=str(empty_config), theme=None)) == "white"


@pytest.mark.parametrize("detail_level", ["simplified", "detailed"])
def test_ecs_templates_change_only_fill_values(tmp_path: Path, detail_level: str) -> None:
    component, edges = _ecs_fixture()
    white_path = tmp_path / f"white-{detail_level}.d2"
    pastel_path = tmp_path / f"pastel-{detail_level}.d2"

    D2Generator(TEMPLATES_DIR, render_theme="white").generate_l3(
        component,
        white_path,
        edges=edges,
        detail_level=detail_level,
        pattern="ecs_microservice",
    )
    D2Generator(TEMPLATES_DIR, render_theme="pastel").generate_l3(
        component,
        pastel_path,
        edges=edges,
        detail_level=detail_level,
        pattern="ecs_microservice",
    )

    white = white_path.read_text()
    pastel = pastel_path.read_text()
    assert 'style.fill: "transparent"' in white
    assert 'style.fill: "#E3F2FD"' in pastel
    assert 'style.fill: "#E8F5E9"' in pastel
    assert 'style.fill: "#FCE4EC"' in pastel
    assert _normalize_fills(white) == _normalize_fills(pastel)


def test_generic_l3_and_l2_honor_theme(tmp_path: Path) -> None:
    component = Component(name="pn-lambda", account="test")
    component.add_node("inputs", _node("pn-lambda-input", "sqs"))
    component.add_node("compute", _node("pn-lambda-worker", "lambda"))
    component.add_node("storage", _node("pn-lambda-table", "dynamodb"))
    component.canonicalize()

    white_l3 = tmp_path / "generic-white.d2"
    pastel_l3 = tmp_path / "generic-pastel.d2"
    D2Generator(TEMPLATES_DIR, "white").generate_l3(
        component, white_l3, pattern="lambda_microservice"
    )
    D2Generator(TEMPLATES_DIR, "pastel").generate_l3(
        component, pastel_l3, pattern="lambda_microservice"
    )
    assert _normalize_fills(white_l3.read_text()) == _normalize_fills(pastel_l3.read_text())

    second = Component(name="pn-second", account="test")
    second.add_node("compute", _node("pn-second-worker", "lambda"))
    graph = ComponentGraph()
    graph.add_component(component)
    graph.add_component(second)
    graph.add_edge(Edge("pn-lambda", "pn-second", "http_call", "HTTP"))

    white_l2 = tmp_path / "l2-white.d2"
    pastel_l2 = tmp_path / "l2-pastel.d2"
    D2Generator(TEMPLATES_DIR, "white").generate_l2(graph, white_l2)
    D2Generator(TEMPLATES_DIR, "pastel").generate_l2(graph, pastel_l2)
    assert _normalize_fills(white_l2.read_text()) == _normalize_fills(pastel_l2.read_text())


@pytest.mark.skipif(shutil.which("d2") is None, reason="D2 CLI is not installed")
def test_white_and_pastel_render_with_identical_geometry(tmp_path: Path) -> None:
    component, edges = _ecs_fixture()
    rendered: dict[str, Path] = {}

    for theme in ("white", "pastel"):
        generator = D2Generator(TEMPLATES_DIR, render_theme=theme)
        d2_path = tmp_path / f"{theme}.d2"
        generator.generate_l3(
            component,
            d2_path,
            edges=edges,
            detail_level="simplified",
            pattern="ecs_microservice",
        )
        rendered[theme] = generator.render(d2_path, fmt="svg")

    assert _geometry_signature(rendered["white"]) == _geometry_signature(rendered["pastel"])


def test_generator_rejects_unknown_theme() -> None:
    with pytest.raises(ValueError, match="Unsupported render theme"):
        D2Generator(TEMPLATES_DIR, render_theme="neon")
