"""Configured references are declarations, not discovered external resources."""
import copy
import json

import pytest

from pndocgen.engine.core import config
from pndocgen.engine.core.view_config import ViewConfig
from pndocgen.engine.core.models import Component, InventoryNode, Edge
from pndocgen.engine.renderers.view import prepare_view
from test_resource_identity import inventory, resolve, QUEUE, ACCOUNT, REGION

BUS = f'arn:aws:events:{REGION}:{ACCOUNT}:event-bus/shared-events'


@pytest.mark.parametrize('kind,aws_kind,identity', [
    ('sqs', 'AWS::SQS::Queue', QUEUE),
    ('aws_events_eventbus', 'AWS::Events::EventBus', BUS),
])
def test_cross_component_reference_reuses_owned_canonical_name(tmp_path, kind, aws_kind, identity):
    data = flow_inventory()
    data['resources'].append(dict(id=identity, name='normalized-owned', type=kind,
        account=ACCOUNT, region=REGION, metadata={'component': 'owner',
        'resource_identity': {'type': aws_kind, 'id': identity}}))
    data['relationships'] = [dict(source='transport', target=identity, type='pipe_flow',
        evidence='pipe_configuration', target_ref={'type': aws_kind, 'id': identity})]
    graph, report = resolve(tmp_path, data)
    assert graph['edges'][0]['target'] == 'normalized-owned'
    reference = next(n for ns in graph['components']['example']['clusters'].values()
                     for n in ns if n['id'] == identity)
    assert reference['name'] == 'normalized-owned'
    assert reference['metadata']['external_reference'] is True
    owned = next(n for ns in graph['components']['owner']['clusters'].values()
                 for n in ns if n['id'] == identity)
    assert owned['name'] == reference['name']
    assert not owned['metadata'].get('external_reference')
    inverted = copy.deepcopy(data)
    inverted['resources'].reverse()
    inverted['relationships'].reverse()
    assert resolve(tmp_path, inverted, name='inverse-owned')[0] == graph


def flow_inventory():
    data = inventory()
    data['relationships'] = [data['relationships'][1], {
        'source': 'transport', 'target': BUS, 'type': 'pipe_flow',
        'label': 'forwards', 'evidence': 'pipe_configuration',
        'target_ref': {'type': 'AWS::Events::EventBus', 'id': BUS}}]
    return data


def prepare(graph, mode='none', **settings):
    data = graph['components']['example']
    comp = Component('example', ACCOUNT)
    for cluster, nodes in data['clusters'].items():
        for n in nodes:
            comp.add_node(cluster, InventoryNode(n['id'], n['name'], n['type'], ACCOUNT, REGION,
                                                 metadata=n.get('metadata', {})))
    edges = [Edge(e['source'], e['target'], e['type'], e.get('label', ''),
                  e.get('evidence', ''), e.get('source_ref'), e.get('target_ref'))
             for e in graph['edges']]
    policy = ViewConfig.from_mapping({'external_references': mode, **settings})
    return prepare_view(comp, edges, policy, config.get_app_config().edge_filter, lambda name: name,
                        resolution_scope=graph.get('resolution_scope'))


def test_configured_references_have_concrete_identity_and_are_opt_in(tmp_path):
    graph, report = resolve(tmp_path, flow_inventory())
    added = report['added_resources']
    assert {n['id'] for n in added} == {QUEUE, BUS}
    assert {n['reason'] for n in added} == {'configured_reference'}
    _, edges, hidden = prepare(graph)
    assert not edges
    assert {r['status'] for r in hidden['relationships']} == {'external_reference_omitted'}
    _, edges, shown = prepare(graph, 'configured')
    assert len(edges) == 2
    assert {r['status'] for r in shown['relationships']} == {'eligible'}
    refs = [n for ns in graph['components']['example']['clusters'].values() for n in ns
            if n.get('metadata', {}).get('external_reference')]
    assert all(n['metadata']['resource_identity']['id'] == n['id'] for n in refs)


def test_external_reference_inversion_and_explicit_exclusions(tmp_path):
    data = flow_inventory()
    graph, _ = resolve(tmp_path, data)
    reverse = copy.deepcopy(data)
    reverse['resources'].reverse()
    reverse['relationships'].reverse()
    assert resolve(tmp_path, reverse, name='inverse')[0] == graph
    _, edges, report = prepare(graph, 'configured', exclusions=[
        dict(component='example', resource_type='sqs', name_prefix='jobs')])
    assert len(edges) == 1
    assert any(r['status'] == 'excluded_endpoint' for r in report['relationships'])


def test_resolve_external_queue_opt_out_wins(tmp_path):
    graph, _ = resolve(tmp_path, flow_inventory(), exclude=True)
    _, edges, report = prepare(graph, 'configured')
    assert len(edges) == 1
    assert not any(n['type'] == 'sqs' for ns in graph['components']['example']['clusters'].values() for n in ns)
    assert any(r.get('endpoint_diagnostics', {}).get('source', {}).get('reason') == 'excluded_external_queues'
               for r in report['relationships'])


def test_iam_alone_does_not_expand_bus_inventory(tmp_path):
    data = flow_inventory()
    data['relationships'] = [dict(data['relationships'][1], evidence='iam_permission',
                                  type='eventbridge_publish_permission')]
    graph, report = resolve(tmp_path, data)
    assert not report['added_resources']
    _, edges, report = prepare(graph, 'configured')
    assert not edges
    assert report['relationships'][0]['endpoint_diagnostics']['target']['reason'] == 'external_reference_not_captured'


def test_unresolved_unsupported_and_excluded_endpoints_are_distinct(tmp_path):
    data = flow_inventory()
    data['relationships'] = [
        dict(source='transport', target='missing', type='pipe_flow', evidence='pipe_configuration'),
        dict(source='transport', target='logger', type='pipe_flow', evidence='pipe_configuration',
             target_ref={'type': 'AWS::Lambda::Function',
                         'id': f'arn:aws:lambda:{REGION}:{ACCOUNT}:function:logger'})]
    graph, _ = resolve(tmp_path, data)
    _, _, report = prepare(graph, 'configured')
    reasons = {r['endpoint_diagnostics']['target']['reason'] for r in report['relationships']}
    assert reasons == {'identity_unresolved', 'unsupported_external_reference_type'}


@pytest.mark.parametrize('field,values', [
    ('external_references', ['all', True, [], None]),
    ('evidence_labels', ['runtime', False, {}, None])])
def test_new_view_options_reject_unknown_values(field, values):
    for value in values:
        with pytest.raises(ValueError):
            ViewConfig.from_mapping({field: value})


def test_new_view_option_defaults():
    assert ViewConfig().external_references == 'none'
    assert ViewConfig().evidence_labels == 'annotated'
    assert ViewConfig.from_mapping({'evidence_labels': 'legacy'}).evidence_labels == 'legacy'


@pytest.mark.parametrize('settings,resource_status,relation_status', [
    ({'excluded_clusters': ['rules']}, 'excluded_auxiliary_cluster', 'excluded_endpoint'),
    ({'purpose_rules': [{'resource_type': 'aws_events_eventbus',
                        'name_prefix': 'shared-', 'purpose': 'logging'}]},
     'excluded_purpose', 'excluded_endpoint'),
    ({'edge_exclusions': [{'component': 'example', 'edge_type': 'pipe_flow',
                          'target': 'shared-events'}]}, 'included', 'excluded_edge_rule')])
def test_configured_references_do_not_bypass_central_filters(
        tmp_path, settings, resource_status, relation_status):
    graph, _ = resolve(tmp_path, flow_inventory())
    original = copy.deepcopy(graph)
    _, edges, report = prepare(graph, 'configured', **settings)
    assert len(edges) == 1
    bus = next(r for r in report['resources'] if r['name'] == 'shared-events')
    assert bus['status'] == resource_status
    relation = next(r for r in report['relationships'] if r['target'] == 'shared-events')
    assert relation['status'] == relation_status
    assert graph == original


@pytest.mark.parametrize('reverse', [False, True])
def test_reference_expansion_stops_after_one_hop(tmp_path, reverse):
    data = flow_inventory()
    second = BUS.replace('shared-events', 'second-hop')
    data['relationships'].append(dict(
        source='shared-events', target=second, type='eventbridge_trigger',
        evidence='eventbridge_target_configuration',
        source_ref={'type': 'AWS::Events::EventBus', 'id': BUS},
        target_ref={'type': 'AWS::Events::EventBus', 'id': second}))
    if reverse:
        data['relationships'].reverse()
    graph, report = resolve(tmp_path, data)
    assert {r['id'] for r in report['added_resources']} == {QUEUE, BUS}
    _, edges, view = prepare(graph, 'configured')
    assert len(edges) == 2
    omitted = next(r for r in view['relationships'] if r['type'] == 'eventbridge_trigger')
    assert omitted['endpoint_diagnostics']['target']['reason'] == 'external_reference_not_captured'


@pytest.mark.parametrize('foreign', [BUS.replace(ACCOUNT, '111111111111'),
                                    BUS.replace(REGION, 'eu-central-1')])
@pytest.mark.parametrize('reverse', [False, True])
def test_bus_homonyms_never_silently_merge(tmp_path, foreign, reverse):
    data = flow_inventory()
    data['relationships'].append(dict(data['relationships'][1], target=foreign,
                                      target_ref={'type': 'AWS::Events::EventBus', 'id': foreign}))
    if reverse:
        data['relationships'].reverse()
    with pytest.raises(ValueError, match='Ambiguous configured reference name'):
        resolve(tmp_path, data)
    assert not (tmp_path / 'graph.json').exists()
