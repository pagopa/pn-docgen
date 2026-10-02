"""Contract tests using explicit in-memory AWS responses; never create AWS clients."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pndocgen.engine.core.config import AppConfig
from pndocgen.sources.aws.cfn_discoverer import CfnLiveDiscoverer
from pndocgen.services import pipeline

REGION = "eu-west-1"
ACCOUNT = "000000000000"
FUNCTION = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:worker"
QUEUE = f"arn:aws:sqs:{REGION}:{ACCOUNT}:queue"
TABLE = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/Table"
TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT}:topic"


class FakeClient:
    def __init__(self, responses, calls, unexpected, service):
        self.responses, self.calls, self.unexpected, self.service = responses, calls, unexpected, service
        self.exceptions = SimpleNamespace(ResourceNotFoundException=type("MissingPolicy", (Exception,), {}))

    def __getattr__(self, operation):
        def invoke(**params):
            self.calls.append((self.service, operation, params))
            if operation not in self.responses:
                self.unexpected.append((self.service, operation, params))
                raise AssertionError(f"Unconfigured fake operation: {self.service}.{operation}")
            response = self.responses[operation]
            return copy.deepcopy(response(params) if callable(response) else response)
        return invoke

    def get_paginator(self, operation):
        def paginate(**params):
            while True:
                page = getattr(self, operation)(**params)
                yield page
                token = page.get("NextMarker") or page.get("NextToken") or (page.get("Marker") if page.get("IsTruncated") else None)
                if not token:
                    break
                params = {**params, ("NextToken" if "NextToken" in page else "Marker"): token}
        return SimpleNamespace(paginate=paginate)


class FakeSession:
    def __init__(self, responses):
        self.calls, self.unexpected = [], []
        self.responses = responses

    def client(self, service, **kwargs):
        return FakeClient(self.responses.get(service, {}), self.calls, self.unexpected, service)


def resource(kind, physical, logical):
    return {"type": "AWS::" + kind, "physical_id": physical, "logical_id": logical}


def discoverer(responses=None, resources=None):
    session = FakeSession(responses or {})
    result = CfnLiveDiscoverer(session, REGION, app_config=AppConfig())
    result.component_resources = {"example": resources or [resource("Lambda::Function", "worker", "Worker")]}
    result.resource_map = {r["physical_id"]: "example" for r in result.component_resources["example"]}
    return result, session


def policy_resources(actions, resources):
    return {"Statement": [{"Effect": "Allow", "Action": actions, "Resource": resources}]}


def iam_responses(doc):
    return {"lambda": {"get_function_configuration": {"Role": f"arn:aws:iam::{ACCOUNT}:role/worker-role"}},
            "iam": {"list_attached_role_policies": {"AttachedPolicies": []},
                    "list_role_policies": {"PolicyNames": ["access"]},
                    "get_role_policy": {"PolicyDocument": doc}}}


def test_api_permission_handles_bare_cfn_function_and_real_api_name():
    doc = {"Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction",
            "Principal": {"Service": "apigateway.amazonaws.com"},
            "Condition": {"ArnLike": {"AWS:SourceArn": f"arn:aws:execute-api:{REGION}:{ACCOUNT}:abc123/*"}}}]}
    discovery, session = discoverer({"lambda": {"get_policy": {"Policy": json.dumps(doc)}}}, [
        resource("Lambda::Function", "worker", "Worker"), resource("ApiGateway::RestApi", "abc123", "Api")])
    discovery.apigw_real_names = {"abc123": "example-public-api"}
    discovery._enrich_apigw_lambda()
    discovery.to_inventory_nodes()
    assert [(e["source"], e["target"]) for e in discovery.to_edges()] == [("example-public-api", "worker")]
    assert not session.unexpected


def test_lambda_mappings_read_all_pages_and_keep_normalized_endpoints():
    raw_queue = "stack-dev-InputQueue-ABCDEFGHIJKL"
    def mappings(params):
        if "Marker" not in params:
            return {"EventSourceMappings": [{"EventSourceArn": QUEUE.rsplit(":", 1)[0] + ":" + raw_queue}], "NextMarker": "page2"}
        assert params["Marker"] == "page2"
        return {"EventSourceMappings": [{"EventSourceArn": TABLE + "/stream/2026-01-01T00:00:00"}]}
    discovery, session = discoverer({"lambda": {"list_event_source_mappings": mappings}}, [
        resource("Lambda::Function", "worker", "Worker"),
        resource("SQS::Queue", f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{raw_queue}", "InputQueue"),
        resource("DynamoDB::Table", "Table", "Table")])
    discovery._enrich_lambda_triggers()
    nodes = discovery.to_inventory_nodes()
    queue = next(n.name for n in nodes if n.resource_type == "sqs")
    assert {(e["source"], e["target"], e["type"]) for e in discovery.to_edges()} == {
        (queue, "worker", "sqs_trigger"), ("Table", "worker", "dynamodb_stream")}
    assert not session.unexpected


def test_lambda_sqs_policy_producer_and_consumer_are_not_lost():
    discovery, session = discoverer(iam_responses(policy_resources(["sqs:SendMessage", "sqs:ReceiveMessage"], QUEUE)))
    discovery._enrich_lambda_iam()
    discovery.to_inventory_nodes()
    assert {(e["source"], e["target"], e["type"]) for e in discovery.to_edges()} == {
        ("worker", "queue", "sqs_producer"), ("queue", "worker", "sqs_consumer")}
    assert not session.unexpected


def test_wildcard_dynamo_policy_does_not_invent_s3_access():
    discovery, session = discoverer(iam_responses(policy_resources("dynamodb:GetItem", "*")), [
        resource("Lambda::Function", "worker", "Worker"), resource("DynamoDB::Table", "Table", "Table"),
        resource("S3::Bucket", "example-bucket", "Bucket")])
    discovery._enrich_lambda_iam()
    assert [(e["target"], e["label"]) for e in discovery.to_edges()] == [("Table", "reads")]
    assert not session.unexpected


def test_lambda_inline_policies_read_all_pages():
    responses = iam_responses(policy_resources("dynamodb:GetItem", TABLE))
    responses["iam"]["list_role_policies"] = lambda p: ({"PolicyNames": [], "IsTruncated": True, "Marker": "next"}
                                                       if "Marker" not in p else {"PolicyNames": ["access"], "IsTruncated": False})
    discovery, session = discoverer(responses, [resource("Lambda::Function", "worker", "Worker"),
                                               resource("DynamoDB::Table", "Table", "Table")])
    discovery._enrich_lambda_iam()
    assert len(discovery.to_edges()) == 1
    assert not session.unexpected


def test_sns_pages_and_lambda_alias_reference_function_not_alias():
    def subscriptions(params):
        if "NextToken" not in params:
            return {"Subscriptions": [], "NextToken": "next"}
        return {"Subscriptions": [{"Protocol": "lambda", "Endpoint": FUNCTION + ":live", "SubscriptionArn": TOPIC + ":uuid"}]}
    discovery, session = discoverer({"sns": {"list_subscriptions_by_topic": subscriptions}}, [
        resource("Lambda::Function", "worker", "Worker"), resource("SNS::Topic", TOPIC, "Topic")])
    discovery._enrich_sns_subscriptions()
    discovery.to_inventory_nodes()
    assert [(e["source"], e["target"]) for e in discovery.to_edges()] == [("topic", "worker")]
    assert not session.unexpected


def test_duplicate_display_names_do_not_silently_drop_resources():
    discovery, _ = discoverer(resources=[resource("Lambda::Function", "worker", "Worker"),
                                         resource("S3::Bucket", "worker", "Bucket")])
    with pytest.raises(ValueError, match="identity|name|Ambiguous"):
        discovery.to_inventory_nodes()


def test_external_sqs_reference_is_attributed_to_lambda_owner(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"region": REGION, "resources": [
        {"id": FUNCTION, "name": "worker", "type": "lambda", "account": "local", "region": REGION,
         "metadata": {"component": "example"}}],
        "relationships": [{"source": "worker", "target": "external-queue", "type": "sqs_producer"}]}))
    output = tmp_path / "graph.json"
    pipeline.resolve(SimpleNamespace(inventory=str(inventory), config=None, output=str(output)))
    graph = json.loads(output.read_text())
    names = {n["name"] for cluster in graph["components"]["example"]["clusters"].values() for n in cluster}
    assert names == {"worker", "external-queue"}


def complete_lambda_inventory(reverse=False, return_session=False):
    """Synthetic AWS-response contract fixture, not a captured live deployment."""
    resources = [resource("Lambda::Function", "worker", "Worker"),
                 resource("SQS::Queue", f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/queue", "Queue"),
                 resource("DynamoDB::Table", "Table", "Table"), resource("S3::Bucket", "example-bucket", "Bucket"),
                 resource("ApiGateway::RestApi", "abc123", "Api"), resource("SNS::Topic", TOPIC, "Topic"),
                 resource("Events::Rule", "refresh-rule", "RefreshRule")]
    if reverse:
        resources.reverse()
    policy = {"Statement": [
        {"Effect": "Allow", "Action": "dynamodb:GetItem", "Resource": TABLE},
        {"Effect": "Allow", "Action": "s3:PutObject", "Resource": "arn:aws:s3:::example-bucket/*"},
        {"Effect": "Allow", "Action": "sqs:SendMessage", "Resource": QUEUE.rsplit(":", 1)[0] + ":external-queue"},
        {"Effect": "Deny", "Action": "sqs:SendMessage", "Resource": QUEUE.rsplit(":", 1)[0] + ":denied-queue"},
    ]}
    responses = iam_responses(policy)
    responses["resourcegroupstaggingapi"] = {"get_resources": {"ResourceTagMappingList": [{
        "ResourceARN": f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/example/uuid",
        "Tags": [{"Key": "Microservice", "Value": "example"}]}]}}
    responses["cloudformation"] = {"list_stack_resources": {"StackResourceSummaries": [
        {"ResourceType": r["type"], "PhysicalResourceId": r["physical_id"], "LogicalResourceId": r["logical_id"]}
        for r in resources]}}
    mappings = [{"EventSourceArn": QUEUE, "State": "Enabled"},
                {"EventSourceArn": TABLE + "/stream/2026-01-01", "State": "Enabled"}]
    responses["lambda"]["list_event_source_mappings"] = {"EventSourceMappings": list(reversed(mappings)) if reverse else mappings}
    responses["lambda"]["get_policy"] = {"Policy": json.dumps({"Statement": [{
        "Effect": "Allow", "Principal": {"Service": "apigateway.amazonaws.com"},
        "Action": "lambda:InvokeFunction",
        "Condition": {"ArnLike": {"AWS:SourceArn": f"arn:aws:execute-api:{REGION}:{ACCOUNT}:abc123/*"}}}]})}
    responses["apigateway"] = {"get_rest_api": {"name": "example-api"}}
    responses["events"] = {"list_targets_by_rule": {"Targets": [{"Id": "worker", "Arn": FUNCTION + ":live"}]}}
    subscriptions = [{"Protocol": "sqs", "Endpoint": QUEUE, "SubscriptionArn": TOPIC + ":one"},
                     {"Protocol": "lambda", "Endpoint": FUNCTION + ":live", "SubscriptionArn": TOPIC + ":two"}]
    responses["sns"] = {"list_subscriptions_by_topic": {"Subscriptions": list(reversed(subscriptions)) if reverse else subscriptions}}
    responses["sqs"] = {"get_queue_attributes": {"Attributes": {}}}
    session = FakeSession(responses)
    discovery = CfnLiveDiscoverer(session, REGION, app_config=AppConfig())
    discovery.discover("example")
    nodes = discovery.to_inventory_nodes(account="local")
    edges = discovery.to_edges()
    assert not session.unexpected
    assert not discovery.issues
    inventory = {"region": REGION, "discovery_source": "synthetic_aws_responses", "relationships": edges,
                 "resources": [{"id": n.id, "name": n.name, "type": n.resource_type, "account": n.account,
                                "region": n.region, "tags": n.tags, "metadata": n.metadata} for n in nodes]}
    expected = {("queue", "worker", "sqs_trigger"), ("Table", "worker", "dynamodb_stream"),
                ("worker", "Table", "storage_access"), ("worker", "example-bucket", "storage_access"),
                ("worker", "external-queue", "sqs_producer"), ("example-api", "worker", "apigw_integration"),
                ("refresh-rule", "worker", "eventbridge_trigger"), ("topic", "queue", "sns_subscription"),
                ("topic", "worker", "sns_subscription")}
    assert {(e["source"], e["target"], e["type"]) for e in edges} == expected
    assert len(edges) == len(expected) == 9
    assert len(nodes) == 7
    return inventory, session if return_session else session.calls


@pytest.mark.parametrize("detail", ["simplified", "detailed"])
def test_full_lambda_discovery_resolve_render_preserves_all_relations(tmp_path, detail):
    from pndocgen.engine.renderers.svg_geometry import Diagram, collisions, outside_container, outside_viewbox
    signatures = []
    for i, reverse in enumerate((False, True)):
        case = tmp_path / str(i)
        case.mkdir()
        inventory, _ = complete_lambda_inventory(reverse)
        source = case / "inventory.json"
        source.write_text(json.dumps(inventory))
        output = case / "graph.json"
        pipeline.resolve(SimpleNamespace(inventory=str(source), config=None, output=str(output)))
        graph = json.loads(output.read_text())
        assert len(graph["edges"]) == 9
        assert graph["components"]["example"]["clusters"]["sns"][0]["type"] == "sns"
        assert all(n["type"] == "sqs" for n in graph["components"]["example"]["clusters"]["queues"])
        resolution = json.loads(output.with_suffix(".resolve.json").read_text())
        assert len(resolution["resources"]) == 7
        assert len(resolution["relationships"]) == 9
        assert len(resolution["added_resources"]) == 1
        assert all(e.get("evidence") for e in graph["edges"])
        pipeline.generate(SimpleNamespace(graph=str(output), config=None, output_dir=str(case / "render"),
                                          level="l3", render=True, detail_level=detail, _run_ts="fixed"))
        d2 = next((case / "render").rglob("*.d2"))
        svg = d2.with_suffix(".svg")
        report = json.loads(d2.with_suffix(".view.json").read_text())
        assert len(report["resources"]) == 8
        assert all(r["status"] == "included" for r in report["resources"])
        assert len(report["relationships"]) == 9
        expected_status = 'aggregated_cluster_pair' if detail == 'simplified' else 'represented_node_edge'
        assert all(r["status"] == expected_status and r["evidence"] for r in report["relationships"])
        diagram = Diagram.load(svg)
        represented = {tuple(r['d2_endpoints']) for r in report['relationships']}
        assert represented == {(e.source,e.target) for e in diagram.edges}
        assert len(diagram.edges) == (len(represented) if detail == 'simplified' else 9)
        assert not collisions(diagram) and not outside_container(diagram) and not outside_viewbox(diagram)
        signatures.append((output.read_bytes(), d2.read_bytes(), svg.read_bytes()))
    assert signatures[0] == signatures[1]


def test_api_permission_deny_and_wrong_principal_do_not_create_edges():
    for effect, principal in (("Deny", "apigateway.amazonaws.com"), ("Allow", "sns.amazonaws.com")):
        doc = {"Statement": [{"Effect": effect, "Action": "lambda:InvokeFunction",
            "Principal": {"Service": principal},
            "Condition": {"ArnLike": {"AWS:SourceArn": f"arn:aws:execute-api:{REGION}:{ACCOUNT}:abc123/*"}}}]}
        discovery, session = discoverer({"lambda": {"get_policy": {"Policy": json.dumps(doc)}}}, [
            resource("Lambda::Function", "worker", "Worker"), resource("ApiGateway::RestApi", "abc123", "Api")])
        discovery._enrich_apigw_lambda()
        assert not discovery.to_edges()
        assert not session.unexpected


def test_kinesis_mapping_is_preserved():
    stream = f"arn:aws:kinesis:{REGION}:{ACCOUNT}:stream/input-stream"
    discovery, session = discoverer({"lambda": {"list_event_source_mappings": {
        "EventSourceMappings": [{"EventSourceArn": stream}]}}}, [
        resource("Lambda::Function", "worker", "Worker"), resource("Kinesis::Stream", "input-stream", "Stream")])
    discovery._enrich_lambda_triggers()
    discovery.to_inventory_nodes()
    assert discovery.to_edges() == [{"source": "input-stream", "target": "worker", "type": "kinesis_trigger",
                                     "label": "stream", "evidence": "event_source_mapping_configuration"}]
    assert not session.unexpected


def test_dynamo_stream_uses_same_display_name_as_inventory():
    discovery, _ = discoverer(resources=[resource("Lambda::Function", "worker", "Worker"),
                                         resource("DynamoDB::Table", "longtablename", "ReadableTable")])
    discovery.lambda_dynamodb_triggers = [{"lambda_arn": FUNCTION + ":live",
                                           "table_arn": TABLE.replace("Table", "longtablename") + "/stream/123"}]
    nodes = discovery.to_inventory_nodes()
    names = {n.name for n in nodes}
    edge = discovery.to_edges()[0]
    assert edge["source"] in names and edge["target"] in names


def test_valid_single_statement_policy_is_supported():
    doc = policy_resources("dynamodb:GetItem", TABLE)
    doc["Statement"] = doc["Statement"][0]
    discovery, session = discoverer(iam_responses(doc), [resource("Lambda::Function", "worker", "Worker"),
                                                       resource("DynamoDB::Table", "Table", "Table")])
    discovery._enrich_lambda_iam()
    assert len(discovery.to_edges()) == 1 and not discovery.issues
    assert not session.unexpected


def test_partial_read_failure_is_preserved_through_to_view_report(tmp_path):
    def denied(params):
        raise PermissionError("synthetic AccessDenied")
    discovery, session = discoverer({"lambda": {"list_event_source_mappings": denied}})
    discovery._enrich_lambda_triggers()
    assert len(discovery.issues) == 1 and not session.unexpected
    source = tmp_path / "inventory.json"
    source.write_text(json.dumps({"resources": [{"id": FUNCTION, "name": "worker", "type": "lambda",
        "account": "local", "region": REGION, "metadata": {"component": "example"}}],
        "relationships": [], "discovery_issues": discovery.issues}))
    graph = tmp_path / "graph.json"
    pipeline.resolve(SimpleNamespace(inventory=str(source), config=None, output=str(graph)))
    pipeline.generate(SimpleNamespace(graph=str(graph), config=None, output_dir=str(tmp_path / "render"),
                                      level="l3", render=False))
    report = json.loads(next((tmp_path / "render").rglob("*.view.json")).read_text())
    assert report["discovery_issues"] == discovery.issues


def test_attached_policies_read_all_pages_and_report_iam_evidence():
    responses = iam_responses(policy_resources([], []))
    responses["iam"]["list_role_policies"] = {"PolicyNames": []}
    policy_arn = f"arn:aws:iam::{ACCOUNT}:policy/access"
    responses["iam"]["list_attached_role_policies"] = lambda p: (
        {"AttachedPolicies": [], "IsTruncated": True, "Marker": "next"} if "Marker" not in p
        else {"AttachedPolicies": [{"PolicyArn": policy_arn}], "IsTruncated": False})
    responses["iam"]["get_policy"] = {"Policy": {"DefaultVersionId": "v1"}}
    responses["iam"]["get_policy_version"] = {"PolicyVersion": {"Document": policy_resources("s3:GetObject", "arn:aws:s3:::example-bucket/*")}}
    discovery, session = discoverer(responses, [resource("Lambda::Function", "worker", "Worker"),
                                               resource("S3::Bucket", "example-bucket", "Bucket")])
    discovery._enrich_lambda_iam()
    assert discovery.to_edges() == [{"source": "worker", "target": "example-bucket", "type": "storage_access",
                                     "label": "reads", "evidence": "iam_permission"}]
    assert not session.unexpected and not discovery.issues


def test_eventbridge_reads_second_page_and_removes_lambda_qualifier():
    responses = {"events": {"list_targets_by_rule": lambda p: (
        {"Targets": [], "NextToken": "next"} if "NextToken" not in p
        else {"Targets": [{"Id": "worker", "Arn": FUNCTION + ":7"}]})}}
    discovery, session = discoverer(responses, [resource("Lambda::Function", "worker", "Worker"),
                                               resource("Events::Rule", "refresh-rule", "Rule")])
    discovery._enrich_eventbridge_targets()
    assert [(e["source"], e["target"]) for e in discovery.to_edges()] == [("refresh-rule", "worker")]
    assert not session.unexpected and not discovery.issues


def test_pending_sns_subscription_is_not_reported_as_configured_binding():
    discovery, session = discoverer({"sns": {"list_subscriptions_by_topic": {"Subscriptions": [{
        "Protocol": "lambda", "Endpoint": FUNCTION, "SubscriptionArn": "PendingConfirmation"}]}}}, [
        resource("Lambda::Function", "worker", "Worker"), resource("SNS::Topic", TOPIC, "Topic")])
    discovery._enrich_sns_subscriptions()
    assert not discovery.to_edges() and not session.unexpected


def test_wildcard_queue_policy_does_not_invent_a_concrete_queue():
    discovery, session = discoverer(iam_responses(policy_resources("sqs:*", "*")))
    discovery._enrich_lambda_iam()
    assert not discovery.to_edges() and not session.unexpected


def test_discover_command_pipeline_with_in_memory_session_only(tmp_path, monkeypatch):
    import boto3
    expected, source_session = complete_lambda_inventory(return_session=True)
    fake = FakeSession(source_session.responses)
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: fake)
    output = tmp_path / "inventory.json"
    pipeline.discover(SimpleNamespace(discovery_source="cfn", component="example", profile="sso_pn-core-dev",
                                      region=REGION, output=str(output)))
    actual = json.loads(output.read_text())
    assert actual["relationships"] == expected["relationships"]
    assert {n["name"] for n in actual["resources"]} == {n["name"] for n in expected["resources"]}
    assert actual["discovery_issues"] == []
    assert not fake.unexpected


def test_resolution_reports_exclusions_and_preserves_existing_outputs(tmp_path):
    source = tmp_path / "inventory.json"
    resources = []
    for name, kind, metadata in (("queue-dlq", "sqs", {"component": "example"}),
                                 ("worker", "lambda", {"component": "example"}),
                                 ("unowned", "lambda", {})):
        resources.append({"id": "test://" + name, "name": name, "type": kind,
                          "account": "local", "region": REGION, "metadata": metadata})
    source.write_text(json.dumps({"resources": resources, "relationships": []}))
    output = tmp_path / "graph.json"
    args = SimpleNamespace(inventory=str(source), config=None, output=str(output))
    pipeline.resolve(args)
    report = json.loads(output.with_suffix(".resolve.json").read_text())
    assert {r["name"]: r["status"] for r in report["resources"]} == {
        "queue-dlq": "excluded_name_pattern", "worker": "included_in_graph", "unowned": "orphan_without_component"}
    previous = output.read_bytes()
    with pytest.raises(FileExistsError):
        pipeline.resolve(args)
    assert output.read_bytes() == previous


@pytest.mark.parametrize("actions,types", [("sqs:GetQueueAttributes", set()),
    ("sqs:Send*", {"sqs_producer"}), ("sqs:ReceiveMessage", {"sqs_consumer"}),
    ("sqs:*", {"sqs_consumer", "sqs_producer"})])
def test_ecs_queue_permissions_are_specific_and_paginated(actions, types):
    ecs_arn = f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/cluster/example"
    responses = iam_responses(policy_resources(actions, QUEUE))
    responses["ecs"] = {"describe_services": {"services": [{"taskDefinition": "task"}]},
                        "describe_task_definition": {"taskDefinition": {"taskRoleArn": f"arn:aws:iam::{ACCOUNT}:role/ecs",
                                                                         "containerDefinitions": []}}}
    responses["iam"]["list_role_policies"] = lambda p: (
        {"PolicyNames": [], "IsTruncated": True, "Marker": "next"} if "Marker" not in p else {"PolicyNames": ["access"]})
    discovery, session = discoverer(responses, [resource("ECS::Service", ecs_arn, "Service")])
    discovery._enrich_ecs_env_vars()
    assert {e["type"] for e in discovery.to_edges()} == types
    assert not session.unexpected and not discovery.issues
