"""Synthetic regression: no credentials, live identifiers or AWS calls."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pndocgen.sources.aws.cfn_discoverer import CfnLiveDiscoverer


def resource(kind, physical_id="example-cache-bucket", logical_id="CacheBucket"):
    return {"type": kind, "physical_id": physical_id, "logical_id": logical_id}


def discoverer(resources):
    session = Mock()
    result = CfnLiveDiscoverer(session, "eu-south-1")
    result.component_resources = {"example": resources}
    return result, session


@pytest.mark.parametrize("reverse", [False, True])
def test_bucket_policy_is_not_a_node(reverse):
    resources = [resource("AWS::S3::Bucket"),
                 resource("AWS::S3::BucketPolicy", logical_id="CacheBucketPolicy")]
    if reverse:
        resources.reverse()
    instance, session = discoverer(resources)
    before = deepcopy(instance.component_resources)
    nodes = instance.to_inventory_nodes(account="local")
    assert [(n.id, n.name, n.resource_type) for n in nodes] == [
        ("example-cache-bucket", "example-cache-bucket", "s3")]
    assert instance.component_resources == before
    assert not session.client.return_value.mock_calls


def test_explicit_include_all_still_detects_collision():
    instance, _ = discoverer([resource("AWS::S3::Bucket"), resource("AWS::S3::BucketPolicy")])
    with pytest.raises(ValueError, match="Ambiguous resource name"):
        instance.to_inventory_nodes(account="local", skip_cfn_types=frozenset())


def test_genuine_different_bucket_identities_still_fail():
    instance, _ = discoverer([
        resource("AWS::S3::Bucket", "example-cache-bucket"),
        resource("AWS::S3::Bucket", "arn:aws:s3:::example-cache-bucket"),
    ])
    with pytest.raises(ValueError, match="distinct physical identities"):
        instance.to_inventory_nodes(account="local")


def test_duplicate_bucket_is_deduplicated():
    instance, _ = discoverer([resource("AWS::S3::Bucket"), resource("AWS::S3::Bucket")])
    assert len(instance.to_inventory_nodes(account="local")) == 1


def test_policy_alone_and_custom_skip_override():
    instance, _ = discoverer([resource("AWS::S3::BucketPolicy")])
    assert instance.to_inventory_nodes(account="local") == []
    instance, _ = discoverer([resource("AWS::S3::BucketPolicy")])
    assert len(instance.to_inventory_nodes(account="local", skip_cfn_types=frozenset())) == 1


def test_pipeline_saves_inventory_with_bucket_not_policy(tmp_path, monkeypatch):
    import boto3
    from pndocgen.services import pipeline

    monkeypatch.setattr(boto3, "Session", lambda **kwargs: Mock())

    def fake_discover(instance, component_filter):
        instance.component_resources = {"example": [
            resource("AWS::S3::Bucket"), resource("AWS::S3::BucketPolicy")]}

    monkeypatch.setattr(CfnLiveDiscoverer, "discover", fake_discover)
    monkeypatch.setattr(CfnLiveDiscoverer, "to_edges", lambda self: [])
    output = tmp_path / "inventory.json"
    pipeline.discover(SimpleNamespace(discovery_source="cfn", component="example",
        profile="local-test", region="eu-south-1", skip_types=None, output=str(output)))
    inventory = json.loads(output.read_text())
    assert [node["type"] for node in inventory["resources"]] == ["s3"]
    assert inventory["discovery_issues"] == []
