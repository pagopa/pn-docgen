"""Offline regression tests for the inventory issue-reporting contract."""

import json
from types import SimpleNamespace

import boto3
import pytest

from pndocgen.services import pipeline


@pytest.mark.parametrize("source", ["tag", "both"])
def test_legacy_discovery_modes_do_not_reference_cfn_only_issues(
    tmp_path, monkeypatch, source
):
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: object())
    monkeypatch.setattr(pipeline, "build_discovery_strategy", lambda *args, **kwargs: object())

    class FakeDiscoverer:
        account = "local"
        env = "test"

        def __init__(self, **kwargs):
            pass

        def discover(self):
            return []

    monkeypatch.setattr("pndocgen.sources.aws.discoverer.AWSDiscoverer", FakeDiscoverer)
    output = tmp_path / "inventory.json"
    pipeline.discover(
        SimpleNamespace(
            discovery_source=source,
            component=None,
            profile="local-test",
            region="eu-west-1",
            prefix="",
            output=str(output),
        )
    )
    inventory = json.loads(output.read_text())
    assert inventory["discovery_source"] == source
    assert inventory["resources"] == []
    assert "discovery_issues" not in inventory


def test_empty_cfn_issue_list_survives_resolve_and_generate(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({
        "discovery_source": "cfn",
        "resources": [{
            "id": "test://worker",
            "name": "worker",
            "type": "lambda",
            "account": "local",
            "region": "eu-west-1",
            "metadata": {"component": "example"},
        }],
        "relationships": [],
        "discovery_issues": [],
    }))
    graph = tmp_path / "graph.json"
    pipeline.resolve(SimpleNamespace(inventory=str(inventory), config=None, output=str(graph)))
    assert json.loads(graph.read_text())["discovery_issues"] == []

    output_dir = tmp_path / "render"
    pipeline.generate(SimpleNamespace(
        graph=str(graph),
        config=None,
        output_dir=str(output_dir),
        level="l3",
        render=False,
    ))
    reports = list(output_dir.rglob("*.view.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text())["discovery_issues"] == []
