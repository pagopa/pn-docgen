"""Multi-service ownership contracts, using only synthetic AWS responses."""
import socket
from pathlib import Path

import boto3
import pytest

from pndocgen.engine.core.models import Edge
from pndocgen.engine.renderers.d2_generator import D2Generator
from pndocgen.engine.resolvers.tag_resolver import TagResolver
from test_lambda_fidelity import ACCOUNT, REGION, discoverer, resource, policy_resources


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Real network/AWS access forbidden')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(boto3, 'Session', forbidden)
    monkeypatch.setattr(boto3.session.Session, 'client', forbidden)


def service(name, cluster='cluster'):
    return resource('ECS::Service', f'arn:aws:ecs:{REGION}:{ACCOUNT}:service/{cluster}/{name}', 'ECSService')


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('names', [('nested-dev-A-ECSService-AAA', 'nested-dev-B-ECSService-BBB'),
                                  ('worker-a', 'worker-b')])
def test_multiple_ecs_names_are_distinct_and_order_independent(reverse, names):
    records = [service(n) for n in names]
    d, _ = discoverer(resources=records[::-1] if reverse else records)
    nodes = d.to_inventory_nodes()
    assert {n.id: n.name for n in nodes} == {r['physical_id']: n for r, n in zip(records, names)}


def test_same_service_name_in_distinct_clusters_and_duplicate_record():
    records = [service('worker', 'cluster-a'), service('worker', 'cluster-b')]
    d, _ = discoverer(resources=records + records[:1])
    nodes = d.to_inventory_nodes()
    assert {n.name for n in nodes} == {'cluster-a/worker', 'cluster-b/worker'}
    assert len(nodes) == 2


def test_single_ecs_generated_name_stays_unchanged():
    d, _ = discoverer(resources=[service('nested-dev-A-ECSService-AAA')])
    node, = d.to_inventory_nodes()
    assert node.name == 'example'


def multi_capture(reverse=False, wildcard=False):
    records = [service('worker-a'), service('worker-b')]
    for label in ('a', 'b'):
        records.extend([resource('DynamoDB::Table', 'Table' + label, 'Table' + label),
                        resource('S3::Bucket', 'bucket-' + label, 'Bucket' + label),
                        resource('SQS::Queue', f'https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/queue-{label}', 'Queue' + label)])

    def describe_services(p):
        name, = p['services']
        return {'services': [{'serviceArn': service(name)['physical_id'], 'taskDefinition': name}]}

    def describe_task(p):
        label = p['taskDefinition'][-1]
        return {'taskDefinition': {'taskRoleArn': f'arn:aws:iam::{ACCOUNT}:role/role-{label}',
                'containerDefinitions': [{'environment': [
                    {'name': 'PN_WORKER_OTHER_BASEURL', 'value': 'http://other'},
                    {'name': 'DATA_TABLE', 'value': 'Table' + label}]}]}}

    def role_policy(p):
        label = p['RoleName'][-1]
        return {'PolicyDocument': {'Statement': [
            policy_resources(['sqs:ReceiveMessage', 'sqs:SendMessage'],
                             f'arn:aws:sqs:{REGION}:{ACCOUNT}:queue-{label}')['Statement'][0],
            policy_resources('dynamodb:GetItem' if label == 'a' else 'dynamodb:PutItem',
                             '*' if wildcard else f'arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/Table{label}')['Statement'][0],
            policy_resources('s3:GetObject' if label == 'a' else 's3:PutObject',
                             '*' if wildcard else f'arn:aws:s3:::bucket-{label}/*')['Statement'][0]]}}

    responses = {'ecs': {'describe_services': describe_services, 'describe_task_definition': describe_task},
                 'iam': {'list_attached_role_policies': {'AttachedPolicies': []},
                         'list_role_policies': {'PolicyNames': ['access']}, 'get_role_policy': role_policy}}
    d, session = discoverer(responses, records[::-1] if reverse else records)
    d._enrich_ecs_env_vars()
    nodes = d.to_inventory_nodes()
    assert not session.unexpected and not d.issues
    return d, nodes


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('wildcard', [False, True])
def test_live_enrichment_keeps_each_service_ownership(reverse, wildcard):
    d, _ = multi_capture(reverse, wildcard)
    edges = d.to_edges()
    for label in ('a', 'b'):
        name = 'worker-' + label
        queue = 'queue-' + label
        expected_label = 'reads' if label == 'a' else 'writes'
        own = [e for e in edges if e['source'] == name]
        assert any(e['type'] == 'sqs_consumer' and e['source'] == queue and e['target'] == name for e in edges)
        assert {e['target'] for e in own if e['type'] == 'sqs_producer'} == {queue}
        storage = {e['target']: e['label'] for e in own if e['type'] == 'storage_access'}
        targets = ['Tablea', 'Tableb', 'bucket-a', 'bucket-b'] if wildcard else ['Table' + label, 'bucket-' + label]
        assert storage == {target: expected_label for target in targets}
        assert len([e for e in own if e['type'] == 'http_call']) == 1
    # Ownership is retained before inventory conversion; it is not guessed later.
    assert set(d.ecs_storage_access) == {service('worker-a')['physical_id'], service('worker-b')['physical_id']}


def test_unknown_physical_owner_never_becomes_an_edge_endpoint():
    d, _ = multi_capture()
    with pytest.raises(ValueError, match='Unresolved ECS service identity'):
        d._ecs_endpoint(service('missing')['physical_id'])


def test_component_only_legacy_edges_remain_guarded():
    d, _ = multi_capture()
    d.ecs_storage_access = {'example': ['Tablea']}
    with pytest.raises(ValueError, match='Ambiguous ECS owner'):
        d.to_edges()


@pytest.mark.parametrize('count', [1, 2])
def test_api_gateway_inference_requires_unique_service(count):
    records = [service('worker-' + str(i)) for i in range(count)]
    records.append(resource('ApiGateway::RestApi', 'api-id', 'PublicApi'))
    d, _ = discoverer(resources=records)
    d._enrich_apigw_ecs()
    d.to_inventory_nodes()
    edges = d.to_edges()
    if count == 1:
        assert edges[0]['target'] == 'worker-0'
        assert edges[0]['evidence'] == 'same_component_heuristic'
        assert not d.issues
    else:
        assert not edges
        assert [i['stage'] for i in d.issues] == ['ambiguous_apigw_ecs_owner']


@pytest.mark.parametrize('target', ['cluster', 'task', 'external', 'exact'])
def test_eventbridge_does_not_match_service_by_substring(target):
    records = [service('example-worker'), service('worker-b'), resource('Events::Rule', 'tick', 'Tick')]
    arn = {'cluster': f'arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/example',
           'task': f'arn:aws:ecs:{REGION}:{ACCOUNT}:task/example-worker/uuid',
           'external': service('example-worker')['physical_id'].replace(ACCOUNT, '111111111111'),
           'exact': records[0]['physical_id']}[target]
    d, session = discoverer({'events': {'describe_rule': {}, 'list_targets_by_rule': {'Targets': [{'Arn': arn}]}}}, records)
    d._enrich_eventbridge_targets()
    d.to_inventory_nodes()
    edges = d.to_edges()
    if target == 'exact':
        assert edges[0]['target'] == 'example-worker'
        assert not d.issues
    else:
        assert not edges
        assert [i['stage'] for i in d.issues] == ['unresolved_eventbridge_ecs_target']
    assert not session.unexpected


@pytest.mark.parametrize('detail', ['simplified', 'detailed'])
def test_multi_ecs_renders_deterministically(detail, tmp_path):
    d, nodes = multi_capture()
    graph = TagResolver().resolve(nodes)
    component = graph.components['example']
    edges = [Edge(e['source'], e['target'], e['type'], e['label']) for e in d.to_edges() if e['type'] != 'http_call']
    component.canonicalize()
    templates = Path(__file__).resolve().parents[1] / 'pndocgen/engine/d2/templates'
    generator = D2Generator(templates)
    first = generator.generate_l3(component, tmp_path / 'first.d2', edges,
                                  pattern='ecs_microservice', detail_level=detail)
    svg = generator.render(first)
    second = generator.generate_l3(component, tmp_path / 'second.d2', edges,
                                   pattern='ecs_microservice', detail_level=detail)
    assert first.read_bytes() == second.read_bytes()
    assert svg.read_bytes() == generator.render(second).read_bytes()
    assert 'worker-a' in svg.read_text() and 'worker-b' in svg.read_text()
