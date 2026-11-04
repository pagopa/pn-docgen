import json
from pathlib import Path
import pytest

from pndocgen.engine.core import config
from pndocgen.engine.core.models import Edge, ComponentGraph
from pndocgen.engine.core.evidence import display_label, bidirectional_pairs
from pndocgen.engine.renderers.d2_generator import D2Generator
from test_lambda_fidelity import discoverer, iam_responses, policy_resources
from test_configured_references import BUS, flow_inventory, resolve
from test_release_contract import service_graph
from test_lambda_fidelity import resource, ACCOUNT, REGION


@pytest.mark.parametrize('action', ['events:PutEvents', 'events:Put*', 'events:*'])
def test_ecs_putevents_uses_task_role_policy_and_preserves_condition(action):
    ecs = f'arn:aws:ecs:{REGION}:{ACCOUNT}:service/cluster/example'
    policy = policy_resources(action, BUS)
    condition = {'StringEquals': {'events:source': 'synthetic.example'}}
    policy['Statement'][0]['Condition'] = condition
    responses = iam_responses(policy)
    responses['ecs'] = {
        'describe_services': {'services': [{'taskDefinition': 'task'}]},
        'describe_task_definition': {'taskDefinition': {
            'taskRoleArn': f'arn:aws:iam::{ACCOUNT}:role/ecs', 'containerDefinitions': []}}}
    d, session = discoverer(responses, [resource('ECS::Service', ecs, 'Service')])
    d._enrich_ecs_env_vars()
    nodes = d.to_inventory_nodes()
    edges = d.to_edges()
    assert len(edges) == 1
    edge = edges[0]
    assert edge['type'] == 'eventbridge_publish_permission'
    assert edge['source'] in {n.name for n in nodes}
    assert edge['target_ref'] == {'type': 'AWS::Events::EventBus', 'id': BUS}
    assert edge['evidence'] == 'iam_permission'
    assert edge['evidence_details'] == {'condition': condition}
    assert not session.unexpected and not d.issues
    assert not any(service == 'events' for service, _, _ in session.calls)


@pytest.mark.parametrize('evidence', ['', 'iam_permission', 'environment_reference'])
def test_l2_labels_remain_independent_of_l3_evidence_mode(tmp_path, evidence):
    graph = service_graph()
    graph.add_edge(Edge('pn-a', 'pn-b', 'http_call', 'HTTP', evidence))
    original = graph.to_dict()
    generator = D2Generator(Path(__file__).parents[1] / 'pndocgen/engine/d2/templates')
    outputs = []
    for mode in ('legacy', 'annotated'):
        config.get_app_config().render.view.evidence_labels = mode
        path = tmp_path / (mode + '.d2')
        generator.generate_l2(graph, path)
        outputs.append(path.read_bytes())
    assert outputs[0] == outputs[1]
    assert graph.to_dict() == original


def test_permission_and_configuration_cannot_collapse_into_confirmed_flow():
    receive = Edge('jobs', 'worker', 'sqs_consumer', 'consumes', 'iam_permission')
    send = Edge('worker', 'jobs', 'sqs_producer', 'produces', 'event_source_mapping_configuration')
    assert not bidirectional_pairs([receive, send])
    assert bidirectional_pairs([receive, send], legacy=True) == {('jobs', 'worker')}
    send.evidence = 'iam_permission'
    assert bidirectional_pairs([receive, send]) == {('jobs', 'worker')}
    assert display_label(receive, bidirectional=True) == 'IAM: send/receive'
    send.evidence_details = {'condition': {'StringEquals': {'events:source': 'example'}}}
    assert not bidirectional_pairs([receive, send])


def test_annotated_labels_do_not_promote_unknown_or_mixed_evidence():
    assert display_label(Edge('a', 'b', 'storage_access', 'reads/writes', 'iam_permission')) == 'IAM: read/write'
    assert display_label(Edge('a', 'b', 'storage_access', 'reads', 'iam_or_environment_reference')) == 'mixed: reads'
    assert display_label(Edge('a', 'b', 'sqs_producer', 'produces')) == 'unspecified: produces'
    assert display_label(Edge('a', 'b', 'sqs_producer', 'sends'), legacy=True) == 'produces'


def test_node_and_cluster_aggregation_preserve_distinct_evidence():
    edges = [Edge('worker', 'jobs', 'sqs_producer', 'produces', e)
             for e in ('iam_permission', 'event_source_mapping_configuration')]
    lookup = {'worker': ('ecs', 'worker'), 'jobs': ('queues', 'jobs')}
    result = D2Generator._build_node_edges_v65(edges, lookup)
    assert {e['label'] for e in result} == {'IAM: send', 'config: produces'}
    result = D2Generator._build_box_edges(edges, lookup)
    assert result[0]['label'] == 'IAM: send; config: produces'


@pytest.mark.parametrize('mode', ['legacy', 'annotated'])
def test_same_iam_pair_collapses_with_correct_label_in_both_modes(mode):
    config.get_app_config().render.view.evidence_labels = mode
    edges = [Edge('worker', 'jobs', 'sqs_producer', 'produces', 'iam_permission'),
             Edge('jobs', 'worker', 'sqs_consumer', 'consumes', 'iam_permission')]
    lookup = {'worker': ('ecs', 'worker'), 'jobs': ('queues', 'jobs')}
    result = D2Generator._build_node_edges_v65(edges, lookup)
    assert len(result) == 1
    assert result[0]['label'] == ('consumes/produces' if mode == 'legacy' else 'IAM: send/receive')


def test_conditional_putevents_is_permission_not_a_bus_discovery(tmp_path):
    policy = policy_resources('events:PutEvents', BUS)
    policy['Statement'][0]['Condition'] = {'StringEquals': {'events:source': 'example'}}
    d, session = discoverer(iam_responses(policy))
    d._enrich_lambda_iam()
    d.to_inventory_nodes()
    edge = next(e for e in d.to_edges() if e['type'] == 'eventbridge_publish_permission')
    assert edge['target_ref']['id'] == BUS
    assert edge['evidence'] == 'iam_permission'
    assert edge['evidence_details'] == {'condition': policy['Statement'][0]['Condition']}
    assert not session.unexpected
    data = flow_inventory()
    data['relationships'] = [edge]
    graph, report = resolve(tmp_path, data)
    assert not report['added_resources']
    assert graph['edges'][0]['evidence_details'] == edge['evidence_details']
    assert display_label(Edge('a', 'b', edge['type'], edge['label'], edge['evidence'],
                              evidence_details=edge['evidence_details'])) == 'IAM: PutEvents (conditional)'


def test_wildcard_putevents_is_diagnostic_and_deny_is_not_allow():
    policy = {'Statement': [
        {'Effect': 'Allow', 'Action': 'events:*', 'Resource': '*'},
        {'Effect': 'Deny', 'Action': 'events:PutEvents', 'Resource': BUS}]}
    d, session = discoverer(iam_responses(policy))
    d._enrich_lambda_iam()
    assert not d.eventbridge_publish_permissions
    assert any(i['stage'] == 'unresolved_eventbridge_permission' for i in d.issues)
    assert not session.unexpected


def test_evidence_details_canonicalization_keeps_distinct_conditions():
    first = Edge('a', 'b', 'permission', evidence='iam_permission',
                 evidence_details={'condition': {'B': 2, 'A': 1}})
    second = Edge('a', 'b', 'permission', evidence='iam_permission',
                  evidence_details={'condition': {'A': 1, 'B': 3}})
    graph = ComponentGraph(edges=[second, first])
    graph.canonicalize()
    before = json.dumps(graph.to_dict())
    graph.edges.reverse()
    graph.canonicalize()
    assert json.dumps(graph.to_dict()) == before
    assert len(graph.edges) == 2
