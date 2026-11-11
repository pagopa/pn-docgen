"""Identity regression tests; explicit synthetic AWS replies and offline CLI."""
import copy
import json
from types import SimpleNamespace

import pytest

from test_lambda_fidelity import discoverer, resource, iam_responses, policy_resources
from pndocgen.services import pipeline

REGION = 'eu-west-1'
ACCOUNT = '000000000000'
QUEUE = f'arn:aws:sqs:{REGION}:{ACCOUNT}:jobs'
URL = f'https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/jobs'


@pytest.mark.parametrize('qualified', [False, True])
def test_resolve_lambda_typed_references_never_join_foreign_namesake(tmp_path, qualified):
    data = inventory()
    arn = f'arn:aws:lambda:{REGION}:{ACCOUNT}:function:worker'
    foreign = arn.replace(ACCOUNT, '111111111111')
    data['resources'][0]['metadata']['resource_identity'] = {'type': 'AWS::Lambda::Function', 'id': arn}
    suffix = ':live' if qualified else ''
    data['relationships'] = [dict(source='transport', target='worker', type='pipe_flow',
        evidence='pipe_configuration', target_ref={'type': 'AWS::Lambda::Function', 'id': v + suffix})
        for v in (arn, foreign)]
    graph, _ = resolve(tmp_path, data)
    assert {e['target'] for e in graph['edges']} == {'worker', foreign + suffix}
    reverse = copy.deepcopy(data)
    reverse['resources'].reverse()
    reverse['relationships'].reverse()
    assert resolve(tmp_path, reverse, name='inverse-lambda')[0] == graph


@pytest.mark.parametrize('physical', ['sales|opaque-routing', 'opaque-routing'])
def test_eventbridge_rule_and_target_join_same_physical_identity(physical):
    d, session = discoverer({'events': {'describe_rule': {}, 'list_targets_by_rule': {'Targets': [{'Arn': QUEUE}]}},
                            'sqs': {'get_queue_attributes': {'Attributes': {}}}}, [
        resource('Events::Rule', physical, 'QueueRouting'), resource('SQS::Queue', URL, 'Jobs')])
    d._enrich_eventbridge_targets()
    nodes = d.to_inventory_nodes()
    names = {n.name for n in nodes}
    edge = next(e for e in d.to_edges() if e['type'] == 'eventbridge_trigger')
    assert edge['source'] in names
    assert edge['target'] in names
    assert not session.unexpected


def test_rule_identity_on_two_buses_is_not_collapsed():
    d, _ = discoverer(resources=[resource('Events::Rule', 'a|same', 'First'),
                                 resource('Events::Rule', 'b|same', 'Second')])
    with pytest.raises(ValueError, match='Ambiguous resource name'):
        d.to_inventory_nodes()


@pytest.mark.parametrize('foreign', [QUEUE.replace(ACCOUNT, '111111111111'),
                                    QUEUE.replace(REGION, 'eu-central-1')])
def test_foreign_iam_queue_does_not_join_owned_homonym(foreign):
    d, _ = discoverer(iam_responses(policy_resources('sqs:SendMessage', foreign)), [
        resource('Lambda::Function', 'worker', 'Worker'), resource('SQS::Queue', URL, 'Jobs')])
    d._enrich_lambda_iam()
    d.to_inventory_nodes()
    edge = next(e for e in d.to_edges() if e['type'] == 'sqs_producer')
    assert edge['target'] != 'jobs'
    assert edge['target_ref'] == {'type': 'AWS::SQS::Queue', 'id': foreign}


def inventory():
    def node(identifier, name, kind):
        return dict(id=identifier, name=name, type=kind, account=ACCOUNT, region=REGION,
                    metadata={'component': 'example'})
    return {'account': ACCOUNT, 'env': 'test', 'region': REGION, 'discovery_issues': [],
            'resources': [node('worker', 'worker', 'lambda'), node('transport', 'transport', 'eventbridge_pipe')],
            'relationships': [
                {'source': 'worker', 'target': 'jobs', 'type': 'sqs_producer', 'label': 'produces',
                 'evidence': 'iam_permission', 'target_ref': {'type': 'AWS::SQS::Queue', 'id': QUEUE}},
                {'source': QUEUE, 'target': 'transport', 'type': 'pipe_flow', 'label': 'forwards',
                 'evidence': 'pipe_configuration', 'source_ref': {'type': 'AWS::SQS::Queue', 'id': QUEUE}}]}


def resolve(tmp_path, data, *, exclude=False, name='graph'):
    source = tmp_path / (name + '_inventory.json')
    source.write_text(json.dumps(data))
    output = tmp_path / (name + '.json')
    pipeline.resolve(SimpleNamespace(inventory=str(source), output=str(output), config=None,
                                    include_dlq=False, exclude_external_queues=exclude, repo_path=None))
    return json.loads(output.read_text()), json.loads(output.with_suffix('.resolve.json').read_text())


def test_external_iam_and_pipe_queue_reconcile_and_are_order_independent(tmp_path):
    data = inventory()
    graph, report = resolve(tmp_path, data)
    queue = next(n for c in graph['components'].values() for ns in c['clusters'].values()
                 for n in ns if n['type'] == 'sqs')
    assert queue['id'] == QUEUE
    pipe = next(e for e in graph['edges'] if e['type'] == 'pipe_flow')
    assert pipe['source'] == queue['name'] == 'jobs'
    assert pipe['source_ref']['id'] == QUEUE
    assert next(r for r in report['relationships'] if r['type'] == 'pipe_flow')['input_source'] == QUEUE
    reversed_data = copy.deepcopy(data)
    for field in ('resources', 'relationships'):
        reversed_data[field].reverse()
    other, _ = resolve(tmp_path, reversed_data, name='reversed')
    assert graph == other


def test_owned_url_and_iam_arn_reconcile_without_external_duplicate(tmp_path):
    data = inventory()
    data['resources'].append(dict(id=URL, name='normalized-jobs', type='sqs', account=ACCOUNT,
                                  region=REGION, metadata={'component': 'example'}))
    graph, report = resolve(tmp_path, data)
    assert not report['added_resources']
    assert next(e for e in graph['edges'] if e['type'] == 'sqs_producer')['target'] == 'normalized-jobs'
    assert next(e for e in graph['edges'] if e['type'] == 'pipe_flow')['source'] == 'normalized-jobs'


def test_exclude_external_queues_does_not_reintroduce_pipe_endpoint(tmp_path):
    graph, _ = resolve(tmp_path, inventory(), exclude=True)
    assert not any(n['type'] == 'sqs' for c in graph['components'].values() for ns in c['clusters'].values() for n in ns)
    assert next(e for e in graph['edges'] if e['type'] == 'pipe_flow')['source'] == QUEUE


def test_external_homonym_is_rejected_instead_of_joined(tmp_path):
    data = inventory()
    data['resources'].append(dict(id=URL.replace(ACCOUNT, '111111111111'), name='jobs', type='sqs',
                                  account='111111111111', region=REGION, metadata={'component': 'example'}))
    with pytest.raises(ValueError, match='Ambiguous.*SQS'):
        resolve(tmp_path, data)


def test_invalid_structured_reference_is_rejected(tmp_path):
    data = inventory()
    data['relationships'][0]['target_ref'] = {'type': 'AWS::SQS::Queue', 'id': 'jobs'}
    with pytest.raises(ValueError, match='reference|identity'):
        resolve(tmp_path, data)


def test_graph_generate_preserves_refs_in_view_report(tmp_path):
    graph, _ = resolve(tmp_path, inventory())
    output = tmp_path / 'diagrams'
    pipeline.generate(SimpleNamespace(graph=str(tmp_path / 'graph.json'), output_dir=str(output),
                                     config=None, level='l3', component=None, pattern='auto',
                                     detail_level='detailed', render=False, render_format='svg'))
    report = json.loads(next(output.glob('*.view.json')).read_text())
    assert next(e for e in report['relationships'] if e['type'] == 'pipe_flow')['source_ref']['id'] == QUEUE


@pytest.mark.parametrize('region,domain,partition', [
    ('eu-west-1', 'amazonaws.com', 'aws'), ('cn-north-1', 'amazonaws.com.cn', 'aws-cn'),
    ('us-gov-west-1', 'amazonaws.com', 'aws-us-gov')])
def test_queue_url_keeps_partition_account_and_region(region, domain, partition):
    from pndocgen.sources.aws.resource_identity import sqs_identity
    assert sqs_identity(f'https://sqs.{region}.{domain}/{ACCOUNT}/jobs.fifo') == (
        f'arn:{partition}:sqs:{region}:{ACCOUNT}:jobs.fifo')


@pytest.mark.parametrize('value', ['jobs', URL.replace('https:', 'http:'), URL + '/extra',
                                  URL.replace('amazonaws.com', 'amazonaws.com.attacker.test'), QUEUE + '*'])
def test_incomplete_or_nonliteral_queue_identity_is_not_guessed(value):
    from pndocgen.sources.aws.resource_identity import sqs_identity
    assert sqs_identity(value) is None


def test_external_identity_metadata_survives_json(tmp_path):
    graph, _ = resolve(tmp_path, inventory())
    queue = next(n for c in graph['components'].values() for ns in c['clusters'].values() for n in ns if n['type'] == 'sqs')
    assert queue['metadata']['resource_identity']['id'] == QUEUE
    assert queue['metadata']['external_reference'] is True


def test_legacy_snapshot_does_not_get_a_guessed_arn(tmp_path):
    data = inventory()
    for rel in data['relationships']:
        rel.pop('source_ref', None)
        rel.pop('target_ref', None)
    graph, _ = resolve(tmp_path, data)
    queue = next(n for c in graph['components'].values() for ns in c['clusters'].values() for n in ns if n['type'] == 'sqs')
    assert queue['id'] == 'external://sqs/jobs'
    assert next(e for e in graph['edges'] if e['type'] == 'pipe_flow')['source'] == QUEUE


def test_eventbridge_targets_with_same_name_and_different_scope_are_not_deduplicated():
    foreign = QUEUE.replace(ACCOUNT, '111111111111')
    d, _ = discoverer({'events': {'list_targets_by_rule': {'Targets': [{'Arn': QUEUE}, {'Arn': foreign}]}},
                       'sqs': {'get_queue_attributes': {'Attributes': {}}}}, [
        resource('Events::Rule', 'sales|routing', 'Routing'), resource('SQS::Queue', URL, 'Jobs')])
    d._enrich_eventbridge_targets()
    d.to_inventory_nodes()
    assert {e['target_ref']['id'] for e in d.to_edges()} == {QUEUE, foreign}


@pytest.mark.parametrize('account,region', [(ACCOUNT, REGION), ('111111111111', REGION), (ACCOUNT, 'eu-central-1')])
def test_rule_policy_fallback_keeps_source_account_and_region(account, region):
    rule_arn = f'arn:aws:events:{region}:{account}:rule/sales/routing'
    policy = {'Statement': [{'Effect': 'Allow', 'Action': 'sqs:SendMessage',
                            'Principal': {'Service': 'events.amazonaws.com'},
                            'Condition': {'ArnEquals': {'aws:SourceArn': rule_arn}}}]}
    d, _ = discoverer({'events': {'list_targets_by_rule': {'Targets': []}},
                       'sqs': {'get_queue_attributes': {'Attributes': {'QueueArn': QUEUE, 'Policy': json.dumps(policy)}}}}, [
        resource('Events::Rule', 'sales|routing', 'Routing'), resource('SQS::Queue', URL, 'Jobs')])
    d.dataflow_scopes.add(('aws', REGION, ACCOUNT))
    d._enrich_eventbridge_targets()
    d.to_inventory_nodes()
    edge = next(e for e in d.to_edges() if e['type'] == 'eventbridge_trigger')
    assert edge['source'] == ('routing' if (account, region) == (ACCOUNT, REGION) else rule_arn)


@pytest.mark.parametrize('principal,expected', [
    ('events.amazonaws.com', True),
    ({'Service': 'events.amazonaws.com'}, True),
    ({'Service': ['lambda.amazonaws.com', 'events.amazonaws.com']}, True),
    ('events.amazonaws.com.attacker.example', False),
    ('attacker-events.amazonaws.com', False),
    ({'Service': 'events.amazonaws.com.attacker.example'}, False),
    ({'Service': ['attacker-events.amazonaws.com']}, False),
    ({'AWS': 'events.amazonaws.com'}, False),
])
def test_rule_policy_requires_exact_eventbridge_service_principal(principal, expected):
    rule_arn = f'arn:aws:events:{REGION}:{ACCOUNT}:rule/sales/routing'
    policy = {'Statement': [{'Effect': 'Allow', 'Action': 'sqs:SendMessage',
                            'Principal': principal,
                            'Condition': {'ArnEquals': {'aws:SourceArn': rule_arn}}}]}
    d, _ = discoverer({'events': {'list_targets_by_rule': {'Targets': []}},
                       'sqs': {'get_queue_attributes': {'Attributes': {
                           'QueueArn': QUEUE, 'Policy': json.dumps(policy)}}}}, [
        resource('Events::Rule', 'sales|routing', 'Routing'),
        resource('SQS::Queue', URL, 'Jobs')])
    d.dataflow_scopes.add(('aws', REGION, ACCOUNT))
    d._enrich_eventbridge_targets()
    d.to_inventory_nodes()
    assert any(e['type'] == 'eventbridge_trigger' for e in d.to_edges()) is expected


def test_sns_foreign_queue_keeps_concrete_scope():
    foreign = QUEUE.replace(ACCOUNT, '111111111111')
    topic = f'arn:aws:sns:{REGION}:{ACCOUNT}:notifications'
    d, _ = discoverer({'sns': {'list_subscriptions_by_topic': {'Subscriptions': [
        {'Protocol': 'sqs', 'Endpoint': foreign, 'SubscriptionArn': topic + ':subscription'}]}}}, [
        resource('SNS::Topic', topic, 'Notifications'), resource('SQS::Queue', URL, 'Jobs')])
    d._enrich_sns_subscriptions()
    d.to_inventory_nodes()
    edge = d.to_edges()[0]
    assert edge['target'] != 'jobs'
    assert edge['target_ref']['id'] == foreign


def test_wildcard_scope_does_not_invent_concrete_sqs_node():
    d, _ = discoverer(iam_responses(policy_resources('sqs:SendMessage', 'arn:aws:sqs:*:*:jobs')))
    d._enrich_lambda_iam()
    assert not d.to_edges()
    assert any(i['stage'] == 'unresolved_sqs_identity' for i in d.issues)


def test_reference_json_member_order_does_not_change_canonical_graph(tmp_path):
    data = inventory()
    resolve(tmp_path, data)
    other = copy.deepcopy(data)
    for rel in other['relationships']:
        for endpoint in ('source_ref', 'target_ref'):
            if endpoint in rel:
                ref = rel[endpoint]
                rel[endpoint] = {'id': ref['id'], 'type': ref['type']}
    resolve(tmp_path, other, name='reordered')
    assert (tmp_path / 'graph.json').read_bytes() == (tmp_path / 'reordered.json').read_bytes()
