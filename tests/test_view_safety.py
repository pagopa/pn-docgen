import json
import shutil
from pathlib import Path

import pytest

from pndocgen.engine.core import config
from pndocgen.engine.core.models import Component, Edge, InventoryNode
from pndocgen.engine.core.view_config import ViewConfig
from pndocgen.engine.renderers.d2_generator import D2Generator
from pndocgen.engine.resolvers.tag_resolver import TagResolver
from test_svg_normalizer import load_graph, ROOT, TEMPLATES, signature


def component(nodes):
    result = Component("example", "local")
    for cluster, name, kind in nodes:
        result.add_node(cluster, InventoryNode("test://" + name, name, kind, "local", "local"))
    return result


@pytest.mark.parametrize("value", [False, [], {"exclusions": {}}, {"typo": []},
    {"exclusions": [{}]}, {"exclusions": [{"component": "x", "resource_type": "lambda", "name_prefix": ""}]},
    {"exclusions": [{"component": "x", "resource_type": "lambda", "name_prefix": False}]}])
def test_invalid_exclusions_rejected(value):
    with pytest.raises(ValueError):
        ViewConfig.from_mapping(value)


@pytest.mark.parametrize("nodes,match", [
    ([("lambdas", "a-b", "lambda"), ("lambdas", "a_b", "lambda")], "identifier collision"),
    ([("lambdas", "same", "lambda"), ("storage", "same", "s3")], "Ambiguous resource name"),
])
def test_ambiguous_identity_fails_before_output(nodes, match, tmp_path):
    with pytest.raises(ValueError, match=match):
        D2Generator(TEMPLATES).generate_l3(component(nodes), tmp_path / "bad.d2")
    assert not list(tmp_path.iterdir())


@pytest.mark.private
def test_delivery_exclusion_is_view_only(tmp_path):
    graph = load_graph(ROOT / "docs/generated/pn_delivery/pn_delivery_202602241109_graph.json")
    before = graph.to_dict()
    cfg = config.get_app_config()
    cfg.render.view = ViewConfig.from_mapping({"exclusions": [{
        "component": "pn-delivery", "resource_type": "lambda", "name_prefix": "pn-delivery-versioning-"}]})
    generator = D2Generator(TEMPLATES)
    d2 = generator.generate_l3(next(iter(graph.components.values())), tmp_path / "delivery.d2",
                               graph.edges, detail_level="detailed")
    report = json.loads(d2.with_suffix(".view.json").read_text())
    excluded = [r for r in report["resources"] if r["status"] == "excluded"]
    assert len(excluded) == 4
    assert "versioning" not in d2.read_text()
    assert graph.to_dict() == before
    assert generator.render(d2).exists()


def test_rule_does_not_leak_to_other_component(tmp_path):
    item = component([("lambdas", "prefix-worker", "lambda")])
    cfg = config.get_app_config()
    cfg.render.view = ViewConfig.from_mapping({"exclusions": [{
        "component": "other", "resource_type": "lambda", "name_prefix": "prefix-"}]})
    generator = D2Generator(TEMPLATES)
    generator.generate_l3(item, tmp_path / "one.d2")
    assert generator.last_view_report["resources"][0]["status"] == "included"


def test_diagram_can_be_moved_without_source_checkout(tmp_path, monkeypatch):
    item = component([("lambdas", "worker", "lambda")])
    generator = D2Generator(TEMPLATES)
    d2 = generator.generate_l3(item, tmp_path / "source/one.d2")
    assert str(ROOT) not in d2.read_text()
    assert "icon: assets/" in d2.read_text()
    expected = signature(generator.render(d2))
    moved = tmp_path / "moved"
    moved.mkdir()
    shutil.copy2(d2, moved / "one.d2")
    shutil.copytree(d2.parent / "assets", moved / "assets")
    monkeypatch.chdir(tmp_path)
    assert signature(generator.render(moved / "one.d2")) == expected


def test_existing_asset_is_never_overwritten(tmp_path):
    from pndocgen.engine.renderers.assets import portable_icons
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"example")
    output = tmp_path / "out/one.d2"
    portable_icons(f"icon: {icon}\n", output, [icon])
    asset = next((output.parent / "assets").iterdir())
    asset.write_bytes(b"keep")
    with pytest.raises(ValueError, match="unexpected content"):
        portable_icons(f"icon: {icon}\n", output, [icon])
    assert asset.read_bytes() == b"keep"


def test_missing_endpoint_and_unsupported_node_are_disclosed(tmp_path):
    item = component([("ecs", "service", "ecs_service"), ("misc", "schedule", "unknown")])
    generator = D2Generator(TEMPLATES)
    generator.generate_l3(item, tmp_path / "one.d2", [Edge("service", "missing", "http_call")])
    assert generator.last_view_report["relationships"][0]["status"] == "endpoint_outside_component"
    assert generator.last_view_report["resources"][1]["status"] == "excluded_auxiliary_cluster"
    generator.generate_l3(item, tmp_path / "two.d2", [Edge("service", "schedule", "http_call")])
    assert generator.last_view_report["relationships"][0]["status"] == "excluded_endpoint"


@pytest.mark.parametrize("existing", ["one.d2", "one.view.json"])
def test_direct_api_preserves_outputs(tmp_path, existing):
    path = tmp_path / existing
    path.write_text("keep")
    with pytest.raises(FileExistsError):
        D2Generator(TEMPLATES).generate_l3(component([]), tmp_path / "one.d2")
    assert path.read_text() == "keep"


def test_relationship_accounting_explains_collapses_and_intra_cluster(tmp_path):
    item = component([("ecs", "service", "ecs_service"), ("queues", "queue", "sqs"),
                      ("lambdas", "a", "lambda"), ("lambdas", "b", "lambda")])
    edges = [Edge("queue", "service", "sqs_consumer"), Edge("service", "queue", "sqs_producer"),
             Edge("a", "b", "http_call")]
    generator = D2Generator(TEMPLATES)
    generator.generate_l3(item, tmp_path / "simple.d2", edges)
    statuses = [r["status"] for r in generator.last_view_report["relationships"]]
    assert statuses.count("aggregated_cluster_pair") == 2
    assert statuses.count("omitted_intra_cluster_simplified") == 1
    generator.generate_l3(item, tmp_path / "detail.d2", edges, detail_level="detailed")
    statuses = [r["status"] for r in generator.last_view_report["relationships"]]
    assert statuses.count("collapsed_bidirectional_sqs") == 1
    assert statuses.count("represented_node_edge") == 2


def test_incident_edges_are_excluded_and_reported(tmp_path):
    item = component([("lambdas", "remove-worker", "lambda"), ("queues", "queue", "sqs")])
    cfg = config.get_app_config()
    cfg.render.view = ViewConfig.from_mapping({"exclusions": [{
        "component": "example", "resource_type": "lambda", "name_prefix": "remove-"}]})
    generator = D2Generator(TEMPLATES)
    generator.generate_l3(item, tmp_path / "filtered.d2", [Edge("queue", "remove-worker", "sqs_trigger")])
    assert generator.last_view_report["relationships"][0]["status"] == "excluded_endpoint"
    assert generator.last_view_report["emitted_edge_count"] == 0


def test_yaml_loads_view_rules(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("render:\n  view:\n    exclusions:\n      - component: example\n        resource_type: lambda\n        name_prefix: versioning-\n")
    assert config.AppConfig.from_yaml(path).render.view.exclusions[0].name_prefix == "versioning-"


@pytest.mark.parametrize("kind", ["lambda", "sqs", "sns", "s3", "dynamodb", "apigw"])
def test_lambda_only_resolve_render_determinism(kind, tmp_path):
    nodes = [InventoryNode("test://worker", "worker", "lambda", "local", "local",
                           metadata={"component": "example"})]
    if kind != "lambda":
        nodes.append(InventoryNode("test://resource", "resource", kind, "local", "local",
                                   metadata={"component": "example"}))
    graph = TagResolver(pattern="auto").resolve(nodes)
    item = graph.components["example"]
    assert D2Generator._detect_pattern(item) == "lambda_microservice"
    edges = [] if kind == "lambda" else [Edge("resource", "worker", "test_relationship", "test")]
    outputs = []
    for detail in ("simplified", "detailed"):
        generator = D2Generator(TEMPLATES)
        d2 = generator.generate_l3(item, tmp_path / f"{detail}.d2", edges, detail_level=detail)
        assert "  elk: {" not in d2.read_text()
        outputs.append(signature(generator.render(d2)))
        assert all(r["status"] == "included" for r in generator.last_view_report["resources"])
        expected_status = 'aggregated_cluster_pair' if detail == 'simplified' else 'represented_node_edge'
        assert all(r["status"] == expected_status for r in generator.last_view_report["relationships"])
        repeated = D2Generator(TEMPLATES)
        repeated_d2 = repeated.generate_l3(item,tmp_path/f'{detail}-repeat.d2',list(reversed(edges)),detail_level=detail)
        assert signature(repeated.render(repeated_d2)) == outputs[-1]
    # Detail levels intentionally differ; determinism is within each contract.
