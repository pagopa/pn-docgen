"""Public, synthetic regression cases. No private captures or AWS access."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pndocgen.engine.core import config
from pndocgen.engine.core.models import Component, Edge, InventoryNode
from pndocgen.engine.renderers.d2_generator import D2Generator
from pndocgen.services import pipeline
from pndocgen.sources.aws.cfn_discoverer import CfnLiveDiscoverer

TEMPLATES = Path(__file__).resolve().parents[1] / "pndocgen/engine/d2/templates"


@pytest.mark.parametrize("cluster,kind,label", [
    ("dynamodb", "storage_access", "reads"),
    ("storage", "storage_access", "writes"),
    ("lambdas", "sqs_consumer", "consumes"),
    ("lambdas", "sqs_trigger", "triggers"),
])
def test_aggregation_preserves_meaning(cluster, kind, label):
    edges = [Edge("a", "b", kind, label)]
    result = D2Generator._build_box_edges(edges, {"a": ("ecs", "a"), "b": (cluster, "b")})
    assert result[0]["label"] == label


def test_aggregation_unions_labels_without_order_dependency():
    edges = [Edge("a", "b", "storage_access", label) for label in ("reads", "writes", "reads")]
    lookup = {"a": ("ecs", "a"), "b": ("dynamodb", "b")}
    assert D2Generator._build_box_edges(edges, lookup) == D2Generator._build_box_edges(list(reversed(edges)), lookup)
    assert D2Generator._build_box_edges(edges, lookup)[0]["label"] == "reads; writes"


def test_sqs_producer_sends_is_displayed_as_produces_without_changing_graph():
    producer = Edge("worker", "q", "sqs_producer", "sends")
    unrelated = Edge("worker", "other", "http_request", "sends")
    lookup = {
        "worker": ("ecs", "worker"),
        "q": ("queues", "q"),
        "other": ("external", "other"),
    }
    box = D2Generator._build_box_edges([producer, unrelated], lookup)
    detailed = D2Generator._build_node_edges_v65([producer, unrelated], lookup)
    assert {edge["to_box"]: edge["label"] for edge in box} == {
        "queues": "produces", "external": "sends",
    }
    assert {edge["to_path"]: edge["label"] for edge in detailed} == {
        "queues.q": "produces", "external.other": "sends",
    }
    assert producer.label == "sends"


def test_missing_explicit_configuration_fails(tmp_path):
    with pytest.raises(FileNotFoundError, match="Explicit configuration"):
        config.get_app_config(tmp_path / "missing.yaml", force_reload=True)


def test_run_loads_configuration_before_first_stage(tmp_path):
    selected = tmp_path / "central.yaml"
    selected.write_text("project:\n  prefix: selected-\n")
    def first_stage(args):
        assert config.get_app_config().project.prefix == "selected-"
        raise RuntimeError("checked before any AWS")
    args = SimpleNamespace(config=str(selected), output_dir=str(tmp_path / "out"), component="example")
    with pytest.raises(RuntimeError, match="checked before any AWS"):
        pipeline.run(args, discover_func=first_stage, resolve_func=Mock(), generate_func=Mock())


def test_empty_explicit_discovery_preserves_capture_and_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("boto3.Session", lambda **kwargs: Mock())
    monkeypatch.setattr(CfnLiveDiscoverer, "discover", lambda *args, **kwargs: None)
    output = tmp_path / "inventory.json"
    args = SimpleNamespace(config=None, output=str(output), component="example", profile="test", region="local", discovery_source="cfn")
    with pytest.raises(ValueError, match="No resources"):
        pipeline.discover(args)
    assert json.loads(output.read_text())["resources"] == []
    assert output.with_suffix(".raw.json").is_file()
    with pytest.raises(FileExistsError):
        pipeline.discover(args)


def test_empty_graph_fails(tmp_path):
    source = tmp_path / "graph.json"
    source.write_text(json.dumps({"components": {}, "edges": []}))
    with pytest.raises(ValueError, match="No diagrams"):
        pipeline.generate(SimpleNamespace(graph=str(source), config=None, output_dir=str(tmp_path / "out"), level="l3", render=False))


def test_component_filter_is_exact(tmp_path):
    source = tmp_path / "graph.json"
    source.write_text(json.dumps({"components": {name: {"clusters": {"lambdas": [
        {"id": name, "name": name, "type": "lambda"}]}} for name in ("example", "example-extra")}, "edges": []}))
    output = tmp_path / "out"
    pipeline.generate(SimpleNamespace(graph=str(source), config=None, component="example", output_dir=str(output), level="l3", render=False))
    assert len(list(output.glob("*.d2"))) == 1


def ecs_capture(count=1):
    capture = CfnLiveDiscoverer(Mock(), "local")
    capture.component_resources = {"example": [
        {"type": "AWS::ECS::Service", "physical_id": f"arn:aws:ecs:local:000000000000:service/cluster/custom-{i}", "logical_id": f"Service{i}"}
        for i in range(count)]}
    capture.ecs_storage_access = {"example": ["Table"]}
    capture.to_inventory_nodes()
    return capture


def test_ecs_endpoint_uses_resource_name_not_component_name():
    assert ecs_capture().to_edges()[0]["source"] == "custom-0"


def test_ambiguous_ecs_owner_is_not_guessed():
    with pytest.raises(ValueError, match="Ambiguous ECS owner"):
        ecs_capture(2).to_edges()


@pytest.mark.parametrize("pattern", ["lambda_microservice", "ecs_microservice"])
@pytest.mark.parametrize("detail", ["simplified", "detailed"])
@pytest.mark.parametrize("theme", ["white", "pastel"])
def test_public_render_roundtrip_with_special_names(tmp_path, pattern, detail, theme):
    component = Component("example", "test")
    for cluster, name, kind in [("apigw", 'Gateway "Public"', "apigw"),
                                ("ecs" if pattern == "ecs_microservice" else "lambdas", "worker", "ecs_service" if pattern == "ecs_microservice" else "lambda"),
                                ("dynamodb", "Table", "dynamodb"), ("storage", "bucket", "s3")]:
        component.add_node(cluster, InventoryNode(name, name, kind, "test", "local"))
    edges = [Edge('Gateway "Public"', "worker", "apigw_integration", 'calls "worker"'),
             Edge("worker", "Table", "storage_access", "reads"),
             Edge("worker", "bucket", "storage_access", "writes")]
    component.canonicalize()
    generator = D2Generator(TEMPLATES, render_theme=theme)
    first = generator.generate_l3(component, tmp_path / "first.d2", edges, pattern=pattern, detail_level=detail)
    svg = generator.render(first)
    second = generator.generate_l3(component, tmp_path / "second.d2", edges, pattern=pattern, detail_level=detail)
    assert first.read_bytes() == second.read_bytes()
    assert svg.read_bytes() == generator.render(second).read_bytes()
    assert 'Gateway \\"Public\\"' in first.read_text()
