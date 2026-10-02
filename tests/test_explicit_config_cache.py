"""Explicit --config must work when the command starts outside the checkout."""
from pathlib import Path
from types import SimpleNamespace
import json
import pytest

from pndocgen.engine.core import config
from pndocgen.services import pipeline


def test_explicit_path_replaces_earlier_default_cache(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.setattr(config, "_cached_config", config.AppConfig())
    assert config.get_app_config().pattern == "auto"

    first = tmp_path / "first.yaml"
    first.write_text("pattern: lambda_microservice\nproject:\n  prefix: first-\n")
    second = tmp_path / "second.yaml"
    second.write_text("pattern: ecs_microservice\nproject:\n  prefix: second-\n")

    assert config.get_app_config(first).pattern == "lambda_microservice"
    assert config.get_app_config(first).project.prefix == "first-"
    assert config.get_app_config(second).pattern == "ecs_microservice"
    assert config.get_app_config(second).project.prefix == "second-"
    assert config.get_app_config().project.prefix == "second-"


@pytest.mark.private
def test_resolve_honors_explicit_config_after_default_cache(tmp_path, monkeypatch, private_root):
    root = private_root
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "_cached_config", config.AppConfig())
    assert not config.get_app_config().resource_kind_overrides

    inventory = root / "docs/generated/pn_delivery/pn_delivery_202602241056_inventory.json"
    target = tmp_path / "graph.json"
    pipeline.resolve(SimpleNamespace(inventory=str(inventory), config=str(root / "pndocgen.yaml"),
                                     output=str(target), pattern="auto", include_dlq=False,
                                     exclude_external_queues=True))

    graph = json.loads(target.read_text())
    types = {node["type"] for comp in graph["components"].values()
             for nodes in comp["clusters"].values() for node in nodes}
    assert "target_group" not in types
    assert "aws_wafv2_webacl" not in types
    assert "target_group" in config.get_app_config().resource_kind_overrides
