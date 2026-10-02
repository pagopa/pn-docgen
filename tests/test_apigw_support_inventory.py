from unittest.mock import Mock

import pytest

from pndocgen.sources.aws.cfn_discoverer import CfnLiveDiscoverer


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("kind", ["Method", "Resource"])
def test_api_support_resources_are_not_separate_nodes(kind, reverse):
    instance = CfnLiveDiscoverer(Mock(), "eu-south-1")
    resources = [
        {"type": "AWS::ApiGateway::RestApi", "physical_id": "abc123def4", "logical_id": "PublicApi"},
        *[{"type": f"AWS::ApiGateway::{kind}", "physical_id": f"api|{name}|GET", "logical_id": name}
          for name in ("First", "Second")],
    ]
    instance.component_resources = {"example": list(reversed(resources)) if reverse else resources}
    nodes = instance.to_inventory_nodes(account="local")
    assert len(nodes) == 1
    assert nodes[0].resource_type == "apigw"
    assert nodes[0].id == "abc123def4"


def test_explicit_override_reports_both_conflicting_identities():
    instance = CfnLiveDiscoverer(Mock(), "eu-south-1")
    instance.component_resources = {"example": [
        {"type": "AWS::ApiGateway::Method", "physical_id": f"api|{name}|GET", "logical_id": name}
        for name in ("First", "Second")]}
    with pytest.raises(ValueError) as error:
        instance.to_inventory_nodes(account="local", skip_cfn_types=frozenset())
    assert all(value in str(error.value) for value in (
        "Ambiguous resource name", "AWS::ApiGateway::Method", "api|First|GET", "api|Second|GET"))
