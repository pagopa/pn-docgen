import json
import pytest

from test_lambda_fidelity import discoverer, resource
from test_resource_identity import ACCOUNT, REGION, QUEUE, URL
from pndocgen.engine.core.models import label_metadata

RULE = f'arn:aws:events:{REGION}:{ACCOUNT}:rule/sales/routing'
BUS = f'arn:aws:events:{REGION}:{ACCOUNT}:event-bus/sales'


@pytest.mark.parametrize('pattern,label', [
    ({'detail-type': ['Completed']}, 'matches Completed'),
    ({'detail-type': ['Completed', 'Failed']}, 'filters events'),
    ({'detail-type': [{'prefix': 'Completed'}]}, 'filters events'),
    ({'detail-type': ['Completed'], 'source': ['example']}, 'filters events')])
def test_rule_view_keeps_full_pattern_and_does_not_simulate_matching(tmp_path, pattern, label):
    from test_resource_identity import resolve
    from test_configured_references import prepare
    d, session = configured_rule({'Arn': RULE, 'EventBusName': 'sales',
                                  'State': 'DISABLED', 'EventPattern': json.dumps(pattern)})
    d._enrich_eventbridge_targets()
    nodes = d.to_inventory_nodes()
    data = {'resources': [dict(id=n.id, name=n.name, type=n.resource_type,
                              account=n.account, region=n.region, metadata=n.metadata) for n in nodes],
            'relationships': d.to_edges()}
    graph, _ = resolve(tmp_path, data)
    _, edges, report = prepare(graph, 'configured')
    association = next(e for e in edges if e.edge_type == 'eventbridge_rule_association')
    assert association.label == label
    rule = next(n for ns in graph['components']['example']['clusters'].values() for n in ns
                if n['type'] == 'eventbridge_rule')
    assert rule['metadata']['eventbridge_rule']['EventPattern'] == pattern
    assert rule['metadata']['eventbridge_rule']['State'] == 'DISABLED'
    assert any(r['type'] == 'eventbridge_trigger' for r in report['relationships'])
    assert not session.unexpected


def configured_rule(describe):
    return discoverer({
        'events': {'describe_rule': describe, 'list_targets_by_rule': {'Targets': [{'Arn': QUEUE}]}},
        'sqs': {'get_queue_attributes': {'Attributes': {}}}},
        [resource('Events::Rule', 'sales|routing', 'Routing'), resource('SQS::Queue', URL, 'Jobs')])


def test_disabled_rule_keeps_configuration_and_bus_identity():
    d, session = configured_rule({'Arn': RULE, 'EventBusName': 'sales', 'State': 'DISABLED',
                                  'EventPattern': json.dumps({'detail-type': ['Completed']})})
    d._enrich_eventbridge_targets()
    nodes = d.to_inventory_nodes()
    rule = next(n for n in nodes if n.resource_type == 'eventbridge_rule')
    assert label_metadata(rule.metadata)['eventbridge_rule'] == {
        'EventBusName': 'sales', 'State': 'DISABLED', 'EventPattern': {'detail-type': ['Completed']}}
    edges = d.to_edges()
    assert {e['type'] for e in edges} == {'eventbridge_trigger', 'eventbridge_rule_association'}
    association = next(e for e in edges if e['type'] == 'eventbridge_rule_association')
    assert association['source_ref'] == {'type': 'AWS::Events::EventBus', 'id': BUS}
    assert association['target'] == rule.name
    assert not session.unexpected
    assert ('events', 'describe_rule', {'Name': 'routing', 'EventBusName': 'sales'}) in session.calls


def test_describe_denied_does_not_discard_targets_or_invent_enabled():
    def denied(params):
        raise PermissionError('AccessDenied')
    d, session = configured_rule(denied)
    d._enrich_eventbridge_targets()
    nodes = d.to_inventory_nodes()
    rule = next(n for n in nodes if n.resource_type == 'eventbridge_rule')
    assert 'eventbridge_rule' not in label_metadata(rule.metadata)
    assert [e['type'] for e in d.to_edges()] == ['eventbridge_trigger']
    assert any(i['stage'] == 'describe_rule' for i in d.issues)
    assert not session.unexpected


def test_stopped_pipe_state_is_preserved_without_implying_traffic():
    from pndocgen.sources.aws.dataflows import enrich
    pipe = f'arn:aws:pipes:{REGION}:{ACCOUNT}:pipe/transport'
    d, session = discoverer({'pipes': {'describe_pipe': {
        'Source': QUEUE, 'Target': BUS, 'CurrentState': 'STOPPED', 'DesiredState': 'STOPPED'}}},
        [resource('Pipes::Pipe', pipe, 'Transport')])
    enrich(d, session)
    node = d.to_inventory_nodes()[0]
    assert label_metadata(node.metadata)['eventbridge_pipe']['CurrentState'] == 'STOPPED'
    assert len(d.to_edges()) == 2
    assert all(e['evidence'] == 'pipe_configuration' for e in d.to_edges())
    assert not session.unexpected


def test_recorded_describe_is_reused_without_another_call():
    d, session = configured_rule({})
    d.dataflow_descriptions['eventbridge:sales|routing'] = {
        '_rule_described': True, 'Arn': RULE, 'EventBusName': 'sales', 'State': 'ENABLED'}
    d._enrich_eventbridge_targets()
    assert not any(operation == 'describe_rule' for _, operation, _ in session.calls)


def test_named_owned_bus_uses_only_captured_scope(tmp_path):
    from test_resource_identity import resolve
    d, _ = configured_rule({'Arn': RULE, 'EventBusName': 'sales', 'State': 'ENABLED'})
    d.component_resources['example'].append(resource('Events::EventBus', 'sales', 'Bus'))
    d.dataflow_scopes.add(('aws', REGION, ACCOUNT))
    d._enrich_eventbridge_targets()
    nodes = d.to_inventory_nodes()
    owned = next(n for n in nodes if n.resource_type == 'aws_events_eventbus')
    assert owned.metadata['resource_identity']['id'] == BUS
    inventory = {'resources': [dict(id=n.id, name=n.name, type=n.resource_type, account=n.account,
                                   region=n.region, metadata=n.metadata) for n in nodes],
                 'relationships': d.to_edges()}
    graph, report = resolve(tmp_path, inventory)
    assert not report['added_resources']
    assert next(e for e in graph['edges'] if e['type'] == 'eventbridge_rule_association')['source'] == owned.name
