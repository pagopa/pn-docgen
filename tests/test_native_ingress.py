from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_svg_normalizer import load_graph, signature, ROOT, FIXTURES, TEMPLATES
from pndocgen.engine.renderers.d2_generator import D2Generator
from pndocgen.engine.core.ingress_config import IngressConfig
from pndocgen.engine.core.config import AppConfig


@pytest.mark.parametrize("fixture", FIXTURES)
@pytest.mark.parametrize("theme", ["white", "pastel"])
@pytest.mark.private
@pytest.mark.legacy_golden
def test_native_generate_matches_validated_prototype(fixture, theme, tmp_path):
    relative, detail, _ = FIXTURES[fixture]
    graph = load_graph(ROOT / "docs/generated" / relative)
    before = graph.to_dict()
    generator = D2Generator(TEMPLATES, render_theme=theme)
    d2 = tmp_path / "native.d2"
    generator.generate_l3(next(iter(graph.components.values())), d2, graph.edges, detail_level=detail)
    svg = generator.render(d2)
    baseline = ROOT.parent / "runs/002"
    if not baseline.is_dir():
        baseline = ROOT / "experiments/014-lambda-fidelity/runs/002"
    expected = baseline / fixture / theme / "elk/diagram.svg"
    assert signature(svg) == signature(expected)
    assert graph.to_dict() == before


def test_config_precedence_and_yaml(tmp_path):
    config = tmp_path / "central.yaml"
    config.write_text("render:\n  ingress:\n    mode: peers\n    components:\n      example: legacy\n")
    policy = AppConfig.from_yaml(config).render.ingress
    assert policy.resolve("other") == ("peers", "common default")
    assert policy.resolve("example") == ("legacy", "component override")
    assert policy.resolve("example", "peers") == ("peers", "CLI")


@pytest.mark.parametrize("value", [{"mode": "bad"}, {"typo": True},
    {"components": {"example": False}}, {"components": []}, False])
def test_invalid_ingress_policy(value):
    with pytest.raises(ValueError):
        IngressConfig.from_mapping(value)
