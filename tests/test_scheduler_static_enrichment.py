"""Offline contract: only explicit local CFN Scheduler targets produce edges."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pndocgen.services import pipeline


def write_case(tmp_path: Path, target: str, *, duplicate_lambda: bool = False):
    repo = tmp_path / "pn-synthetic"
    template = repo / "scripts/aws/cfn/microservice.yml"
    template.parent.mkdir(parents=True)
    template.write_text(f"""
Resources:
  Schedule:
    Type: AWS::Scheduler::Schedule
    Properties:
      Target:
        Arn: {target}
  Worker:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: worker
""")
    resources = [
        {"id": "test://ecs", "name": "service", "type": "ecs_service",
         "account": "local", "region": "local",
         "metadata": {"component": "pn-synthetic", "logical_id": "Service",
                      "cfn_type": "AWS::ECS::Service"}},
        {"id": "test://schedule", "name": "schedule", "type": "aws_scheduler_schedule",
         "account": "local", "region": "local",
         "metadata": {"component": "pn-synthetic", "logical_id": "Schedule",
                      "cfn_type": "AWS::Scheduler::Schedule"}},
        {"id": "test://worker", "name": "worker", "type": "lambda",
         "account": "local", "region": "local",
         "metadata": {"component": "pn-synthetic", "logical_id": "Worker",
                      "cfn_type": "AWS::Lambda::Function"}},
    ]
    if duplicate_lambda:
        resources.append({"id": "test://worker-duplicate", "name": "worker-duplicate",
                          "type": "lambda", "account": "local", "region": "local",
                          "metadata": {"component": "pn-synthetic", "logical_id": "Worker",
                                       "cfn_type": "AWS::Lambda::Function"}})
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"resources": resources, "relationships": []}))
    return repo, inventory


def resolve(tmp_path, inventory, repo=None):
    output = tmp_path / "graph.json"
    pipeline.resolve(SimpleNamespace(inventory=str(inventory),
                                     config=str(Path(__file__).resolve().parents[1] / "pndocgen.yaml"),
                                     repo_path=str(repo) if repo else None,
                                     output=str(output)))
    return json.loads(output.read_text()), json.loads(output.with_suffix(".resolve.json").read_text()), output


def test_explicit_schedule_getatt_joins_inventory_logical_ids_and_renders(tmp_path):
    repo, inventory = write_case(tmp_path, "!GetAtt Worker.Arn")
    graph, report, graph_path = resolve(tmp_path, inventory, repo)
    matches = [edge for edge in graph["edges"] if edge["type"] == "scheduler_trigger"]
    assert matches == [{"source": "schedule", "target": "worker", "type": "scheduler_trigger",
                        "label": "triggers", "evidence": "cfn_template_target_reference"}]
    assert report["static_scheduler_bindings"][0]["status"] == "resolved_into_graph"
    assert [n["name"] for n in graph["components"]["pn-synthetic"]["clusters"]["rules"]] == ["schedule"]

    render = tmp_path / "render"
    pipeline.generate(SimpleNamespace(graph=str(graph_path), config=None, output_dir=str(render),
                                      level="l3", detail_level="detailed", render=False))
    d2 = next(render.glob("*.d2"))
    view = json.loads(d2.with_suffix(".view.json").read_text())
    assert 'rules: "EventBridge Triggers"' in d2.read_text()
    assert next(item for item in view["resources"] if item["name"] == "schedule")["status"] == "included"
    assert next(item for item in view["relationships"] if item["type"] == "scheduler_trigger")["status"] == "represented_node_edge"


def test_without_repo_path_does_not_invent_target_or_change_default_view(tmp_path):
    _, inventory = write_case(tmp_path, "!GetAtt Worker.Arn")
    graph, report, graph_path = resolve(tmp_path, inventory)
    assert not any(edge["type"] == "scheduler_trigger" for edge in graph["edges"])
    assert "static_scheduler_bindings" not in report
    render = tmp_path / "render"
    pipeline.generate(SimpleNamespace(graph=str(graph_path), config=None, output_dir=str(render),
                                      level="l3", detail_level="detailed", render=False))
    view = json.loads(next(render.glob("*.view.json")).read_text())
    assert next(item for item in view["resources"] if item["name"] == "schedule")["status"] == "included"


def test_unsupported_target_expression_abstains_with_report(tmp_path):
    repo, inventory = write_case(tmp_path, "!Sub arn:aws:lambda:local:000000000000:function:worker")
    graph, report, _ = resolve(tmp_path, inventory, repo)
    assert not any(edge["type"] == "scheduler_trigger" for edge in graph["edges"])
    assert report["static_scheduler_bindings"][0]["status"] == "unsupported_target_expression"


def test_ambiguous_inventory_target_abstains_with_report(tmp_path):
    repo, inventory = write_case(tmp_path, "!GetAtt Worker.Arn", duplicate_lambda=True)
    graph, report, _ = resolve(tmp_path, inventory, repo)
    assert not any(edge["type"] == "scheduler_trigger" for edge in graph["edges"])
    assert report["static_scheduler_bindings"][0]["status"] == "ambiguous_target"


def test_untagged_scalar_that_looks_like_getatt_does_not_create_edge(tmp_path):
    repo, inventory = write_case(tmp_path, "Worker.Arn")
    graph, report, _ = resolve(tmp_path, inventory, repo)
    assert not any(edge["type"] == "scheduler_trigger" for edge in graph["edges"])
    assert report["static_scheduler_bindings"][0]["status"] == "unsupported_target_expression"


def test_long_form_getatt_is_supported(tmp_path):
    repo, inventory = write_case(tmp_path, "{Fn::GetAtt: [Worker, Arn]}")
    graph, report, _ = resolve(tmp_path, inventory, repo)
    assert [(edge["source"], edge["target"]) for edge in graph["edges"]
            if edge["type"] == "scheduler_trigger"] == [("schedule", "worker")]
    assert report["static_scheduler_bindings"][0]["status"] == "resolved_into_graph"


def test_duplicate_schedule_logical_id_across_templates_abstains(tmp_path):
    repo, inventory = write_case(tmp_path, "!GetAtt Worker.Arn")
    (repo / "scripts/aws/cfn/storage.yml").write_text("""
Resources:
  Schedule:
    Type: AWS::Scheduler::Schedule
    Properties:
      Target:
        Arn: !GetAtt Worker.Arn
  Worker:
    Type: AWS::Lambda::Function
""")
    graph, report, _ = resolve(tmp_path, inventory, repo)
    assert not any(edge["type"] == "scheduler_trigger" for edge in graph["edges"])
    assert [record["status"] for record in report["static_scheduler_bindings"]] == [
        "ambiguous_template_schedule", "ambiguous_template_schedule"
    ]


def test_lambda_only_service_keeps_scheduler_edge_visible(tmp_path):
    repo, inventory = write_case(tmp_path, "!GetAtt Worker.Arn")
    data = json.loads(inventory.read_text())
    data["resources"] = [node for node in data["resources"] if node["type"] != "ecs_service"]
    inventory.write_text(json.dumps(data))
    graph, report, graph_path = resolve(tmp_path, inventory, repo)
    assert report["static_scheduler_bindings"][0]["status"] == "resolved_into_graph"
    assert any(edge["type"] == "scheduler_trigger" for edge in graph["edges"])

    render = tmp_path / "render"
    pipeline.generate(SimpleNamespace(graph=str(graph_path), config=None, output_dir=str(render),
                                      level="l3", detail_level="detailed", render=True))
    svg = next(render.glob("*.svg"))
    view = json.loads(svg.with_suffix(".view.json").read_text())
    assert next(item for item in view["resources"] if item["name"] == "schedule")["status"] == "included"
    assert next(item for item in view["relationships"] if item["type"] == "scheduler_trigger")["status"] == "represented_node_edge"


def test_static_source_report_discloses_missing_configured_template(tmp_path):
    repo, inventory = write_case(tmp_path, "!GetAtt Worker.Arn")
    _, report, _ = resolve(tmp_path, inventory, repo)
    assert report["static_scheduler_sources"] == [
        {"source_file": "scripts/aws/cfn/microservice.yml", "status": "scanned", "schedule_count": 1,
         "template_sha256": hashlib.sha256(
             (repo / "scripts/aws/cfn/microservice.yml").read_bytes()).hexdigest()},
        {"source_file": "scripts/aws/cfn/storage.yml", "status": "template_missing"},
    ]


def test_binding_records_exact_template_bytes_used_for_static_evidence(tmp_path):
    repo, inventory = write_case(tmp_path, "!GetAtt Worker.Arn")
    _, report, _ = resolve(tmp_path, inventory, repo)
    template = repo / "scripts/aws/cfn/microservice.yml"
    expected = hashlib.sha256(template.read_bytes()).hexdigest()
    assert report["static_scheduler_sources"][0]["template_sha256"] == expected
    assert report["static_scheduler_bindings"][0]["template_sha256"] == expected


def test_comment_only_template_change_updates_provenance_not_graph(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    repo_one, inventory_one = write_case(first, "!GetAtt Worker.Arn")
    repo_two, inventory_two = write_case(second, "!GetAtt Worker.Arn")
    template_two = repo_two / "scripts/aws/cfn/microservice.yml"
    template_two.write_text(template_two.read_text() + "\n# comment-only revision\n")
    graph_one, report_one, _ = resolve(first, inventory_one, repo_one)
    graph_two, report_two, _ = resolve(second, inventory_two, repo_two)
    assert graph_one == graph_two
    assert (report_one["static_scheduler_bindings"][0]["template_sha256"]
            != report_two["static_scheduler_bindings"][0]["template_sha256"])


def test_static_source_report_discloses_malformed_template(tmp_path):
    repo, inventory = write_case(tmp_path, "!GetAtt Worker.Arn")
    (repo / "scripts/aws/cfn/microservice.yml").write_text("Resources: [")
    graph, report, _ = resolve(tmp_path, inventory, repo)
    assert not any(edge["type"] == "scheduler_trigger" for edge in graph["edges"])
    assert report["static_scheduler_bindings"] == []
    assert report["static_scheduler_sources"][0] == {
        "source_file": "scripts/aws/cfn/microservice.yml", "status": "template_parse_error",
        "template_sha256": hashlib.sha256(b"Resources: [").hexdigest(),
    }


def test_static_source_report_discloses_empty_template(tmp_path):
    repo, inventory = write_case(tmp_path, "!GetAtt Worker.Arn")
    (repo / "scripts/aws/cfn/microservice.yml").write_text("")
    _, report, _ = resolve(tmp_path, inventory, repo)
    assert report["static_scheduler_sources"][0]["status"] == "template_invalid_root"


def test_explicit_nonexistent_repo_path_fails_before_writing_graph(tmp_path):
    _, inventory = write_case(tmp_path, "!GetAtt Worker.Arn")
    with pytest.raises(FileNotFoundError, match="Repo path not found"):
        resolve(tmp_path, inventory, tmp_path / "missing-repo")
    assert not (tmp_path / "graph.json").exists()
