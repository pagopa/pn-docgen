"""Local-only tests for lost relationships, output collisions and render failures."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from pndocgen.engine.core import config
from pndocgen.engine.core.config import AppConfig, EdgeFilterConfig
from pndocgen.engine.core.models import Edge
from pndocgen.engine.renderers import d2_generator as rendering
from pndocgen.engine.renderers.d2_generator import D2Generator
from pndocgen.services import pipeline
from pndocgen.sources.aws.cfn_discoverer import CfnLiveDiscoverer

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "pndocgen/engine/d2/templates"


@pytest.mark.parametrize("suffix", ["", "/stream/2020-01-01T00:00:00.000", "/index/TestIndex"])
def test_stream_edges_reference_the_table(suffix):
    # Explicit fake session: no boto3 session or AWS client is created.
    discoverer = CfnLiveDiscoverer(Mock(), "local", app_config=AppConfig())
    discoverer.lambda_dynamodb_triggers = [{
        "table_arn": "arn:aws:dynamodb:local:000000000000:table/TestTable" + suffix,
        "lambda_arn": "TestWorker",
    }]
    assert discoverer.to_edges() == [{"source": "TestTable", "target": "TestWorker", "type": "dynamodb_stream", "label": "stream", "evidence": "event_source_mapping_configuration"}]


def test_unrecognized_stream_is_reported_and_not_fabricated(caplog):
    discoverer = CfnLiveDiscoverer(Mock(), "local", app_config=AppConfig())
    discoverer.lambda_dynamodb_triggers = [{"table_arn": "invalid", "lambda_arn": "worker"}]
    assert discoverer.to_edges() == []
    assert "table name could not be resolved" in caplog.text


@pytest.mark.parametrize("allowed,label", [("sqs_producer", "produces"), ("sqs_consumer", "consumes")])
def test_filter_runs_before_bidirectional_collapse(monkeypatch, allowed, label):
    monkeypatch.setattr(config, "_cached_config", AppConfig(edge_filter=EdgeFilterConfig(include=[allowed])))
    edges = [Edge("q", "worker", "sqs_consumer", "consumes"), Edge("worker", "q", "sqs_producer", "produces")]
    lookup = {"q": ("queues", "q"), "worker": ("ecs", "worker")}
    detailed = D2Generator._build_node_edges_v65(edges, lookup)
    generic = D2Generator._build_data_edges(edges, lookup, "example")
    assert len(detailed) == len(generic) == 1
    assert detailed[0]["label"] == generic[0]["label"] == label


def _args(tmp_path, **overrides):
    values = dict(output_dir=str(tmp_path), component=None, config=None, pattern="auto",
                  include_dlq=False, exclude_external_queues=False, render=False,
                  level="l3", detail_level="simplified")
    values.update(overrides)
    return SimpleNamespace(**values)


def _local_discovery(args):
    Path(args.output).write_text(json.dumps({
        "region": "local", "relationships": [], "resources": [
            {"id": f"test://{name}", "name": name, "type": "ecs_service",
             "account": "test", "region": "local", "metadata": {"component": name}}
            for name in ("service-a", "service-b")
        ],
    }))


def test_multi_component_run_does_not_overwrite_outputs(tmp_path, monkeypatch):
    from datetime import datetime
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 12, 0, tzinfo=tz)
    monkeypatch.setattr("datetime.datetime", FixedDatetime)
    args = _args(tmp_path)
    pipeline.run(args, discover_func=_local_discovery, resolve_func=pipeline.resolve, generate_func=pipeline.generate)
    diagrams = sorted((tmp_path / "all").glob("*.d2"))
    assert len(diagrams) == 2
    assert diagrams[0].name.startswith("service_a_")
    assert diagrams[1].name.startswith("service_b_")
    assert "service-a" in diagrams[0].read_text()
    assert "service-b" in diagrams[1].read_text()
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in (tmp_path / "all").rglob("*") if p.is_file()}
    with pytest.raises(FileExistsError, match="Snapshot already exists"):
        pipeline.run(_args(tmp_path), discover_func=_local_discovery, resolve_func=pipeline.resolve, generate_func=pipeline.generate)
    assert before == {str(p.relative_to(tmp_path)): p.read_bytes() for p in (tmp_path / "all").rglob("*") if p.is_file()}


@pytest.mark.parametrize("failure", ["missing", "timeout", "nonzero", "no-output"])
def test_render_failures_do_not_leave_successful_artifacts(tmp_path, monkeypatch, failure):
    source = tmp_path / "diagram.d2"
    source.write_text("a -> b")
    monkeypatch.setattr(rendering.shutil, "which", lambda name: None if failure == "missing" else "/fake/d2")
    if failure == "timeout":
        call = Mock(side_effect=subprocess.TimeoutExpired("d2", 0.1))
    else:
        call = Mock(return_value=SimpleNamespace(returncode=1 if failure == "nonzero" else 0, stderr="test failure"))
    monkeypatch.setattr(rendering.subprocess, "run", call)
    with pytest.raises(RuntimeError):
        D2Generator(TEMPLATES).render(source, "svg", timeout=0.1)
    assert not source.with_suffix(".svg").exists()
    assert list(tmp_path.iterdir()) == [source]
    if failure != "missing":
        assert call.call_args.kwargs["timeout"] == 0.1


def test_existing_render_is_preserved(tmp_path):
    source = tmp_path / "diagram.d2"
    source.write_text("a -> b")
    target = source.with_suffix(".svg")
    target.write_text("existing result")
    with pytest.raises(FileExistsError):
        D2Generator(TEMPLATES).render(source, "svg")
    assert target.read_text() == "existing result"


def test_pipeline_reports_render_failure_as_failure(tmp_path, monkeypatch):
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"components": {"service": {"account": "test", "clusters": {}}}, "edges": []}))
    monkeypatch.setattr(D2Generator, "render", Mock(side_effect=RuntimeError("render failed")))
    with pytest.raises(RuntimeError, match="1 render\\(s\\) failed"):
        pipeline.generate(_args(tmp_path, graph=str(graph), render=True))


def test_explicit_config_applies_to_filter_as_well_as_theme(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text("render:\n  theme: pastel\nedge_types:\n  include: [sqs_producer]\n")
    assert pipeline._resolve_render_theme(SimpleNamespace(config=str(path), theme=None)) == "pastel"
    assert config.get_app_config().edge_filter.is_allowed("sqs_producer")
    assert not config.get_app_config().edge_filter.is_allowed("sqs_consumer")


@pytest.mark.parametrize("command,extra", [("generate", []), ("run", ["--profile", "test", "--region", "local"])])
def test_cli_defaults_to_svg(monkeypatch, command, extra):
    from pndocgen import cli
    callback = Mock()
    monkeypatch.setattr(cli, f"cmd_{command}", callback)
    monkeypatch.setattr(sys, "argv", ["pn-docgen", command, *extra])
    cli.main()
    assert callback.call_args.args[0].render_format == "svg"
