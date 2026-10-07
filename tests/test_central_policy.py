import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from pndocgen.engine.core.models import Component, InventoryNode, get_resource_meta, ResourceKind
from pndocgen.engine.core.resource_policy import CFN_SUPPORT_TYPES
from pndocgen.engine.renderers.d2_generator import D2Generator


def test_shared_support_policy():
    for kind in CFN_SUPPORT_TYPES:
        assert get_resource_meta(kind.lower().replace("::", "_")).kind == ResourceKind.SKIP


@pytest.mark.parametrize("pattern", ["ecs_microservice", "lambda_microservice"])
def test_same_visibility_for_both_patterns(pattern, tmp_path):
    c = Component("example", "local")
    for name, kind in [("model", "aws_apigateway_model"), ("validator", "aws_apigateway_requestvalidator"),
                       ("unknown", "future_type"), ("schedule", "aws_scheduler_schedule"),
                       ("auth", "aws_apigateway_authorizer")]:
        c.add_node("misc", InventoryNode("test://"+name, name, kind, "local", "local"))
    before = deepcopy(c)
    generator = D2Generator(Path(__file__).parents[1] / "pndocgen/engine/d2/templates")
    path = generator.generate_l3(c, tmp_path / "view.d2", pattern=pattern, detail_level="detailed")
    report = generator.last_view_report
    status = {r["name"]:r for r in report["resources"]}
    assert status["model"]["status"] == status["validator"]["status"] == "excluded_resource_kind"
    assert status["unknown"]["status"] == "excluded_auxiliary_cluster"
    assert "essential view" in status["unknown"]["reason"]
    assert status["schedule"]["cluster"] == "rules"
    assert status["auth"]["cluster"] == "security"
    assert status["auth"]["status"] == "excluded_auxiliary_cluster"
    assert c == before
    assert generator.render(path).exists()


def test_capture_survives_conversion_failure(tmp_path, monkeypatch):
    import boto3
    from pndocgen.services import pipeline
    from pndocgen.sources.aws.cfn_discoverer import CfnLiveDiscoverer
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: Mock())
    def fake_discover(self, component_filter):
        self.component_resources = {"example": [{"type":"example", "physical_id":"one"}]}
    monkeypatch.setattr(CfnLiveDiscoverer, "discover", fake_discover)
    def fail(*args, **kwargs):
        raise ValueError("synthetic conversion failure")
    monkeypatch.setattr(CfnLiveDiscoverer, "to_inventory_nodes", fail)
    args = SimpleNamespace(output=str(tmp_path / "inventory.json"), discovery_source="cfn",
                           component="example", profile="test", region="eu-south-1")
    with pytest.raises(ValueError, match="synthetic"):
        pipeline.discover(args)
    raw = tmp_path / "inventory.raw.json"
    assert json.loads(raw.read_text())["component_resources"]["example"][0]["physical_id"] == "one"
    with pytest.raises(FileExistsError):
        pipeline.discover(args)
