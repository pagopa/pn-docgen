"""Explicit AWS response fixtures: no account, service naming or network dependency."""
from pathlib import Path
from unittest.mock import Mock
import pytest
from pndocgen.sources.aws.cfn_discoverer import CfnLiveDiscoverer
from pndocgen.sources.aws.dataflows import enrich, add_link, resolved_links
from pndocgen.engine.core.models import Component, InventoryNode, Edge
from pndocgen.engine.core.view_config import ViewConfig
from pndocgen.engine.core.config import EdgeFilterConfig
from pndocgen.engine.renderers.view import prepare_view
from pndocgen.engine.renderers.d2_generator import D2Generator

A = 'arn:aws:'
BUCKET = A + 's3tables:eu-west-1:123456789012:bucket/analytics'
STREAM = A + 'firehose:eu-west-1:123456789012:deliverystream/delivery'
PIPE = A + 'pipes:eu-west-1:123456789012:pipe/transport'
QUEUE = A + 'sqs:eu-west-1:123456789012:input'


def subject():
    clients = {name: Mock() for name in ('cloudformation', 'lambda', 'ecs', 'iam', 'sqs',
               'resourcegroupstaggingapi', 'apigateway', 'events', 'sns', 'firehose', 'pipes')}
    session = Mock()
    session.client.side_effect = lambda name, **kw: clients[name]
    d = CfnLiveDiscoverer(session, 'eu-west-1')
    d.component_resources = {'arbitrary-component': [
        {'type': t, 'physical_id': i, 'logical_id': l} for t, i, l in [
            ('AWS::Pipes::Pipe', PIPE, 'Transport'),
            ('AWS::KinesisFirehose::DeliveryStream', STREAM, 'Delivery'),
            ('AWS::S3Tables::TableBucket', BUCKET, 'AnalyticsBucket'),
            ('AWS::S3Tables::Table', BUCKET + '/table/uuid', 'Data'),
            ('AWS::S3::Bucket', 'errors', 'ArbitraryName'),
            ('AWS::SQS::Queue', 'https://sqs.eu-west-1.amazonaws.com/123456789012/input', 'Input')]]}
    clients['pipes'].describe_pipe.return_value = {'Source': QUEUE, 'Target': STREAM}
    clients['firehose'].describe_delivery_stream.return_value = {'DeliveryStreamDescription': {
        'Destinations': [{'IcebergDestinationDescription': {
            'CatalogConfiguration': {'CatalogARN': A+'glue:eu-west-1:123456789012:catalog/s3tablescatalog/analytics'},
            'S3DestinationDescription': {'BucketARN': A+'s3:::errors'}, 'S3BackupMode': 'FailedDataOnly'}}]}}
    return d, session, clients


def test_pipe_firehose_tablebucket_and_backup_use_physical_identity():
    d, session, clients = subject()
    enrich(d, session)
    nodes = d.to_inventory_nodes()
    edges = list(resolved_links(d))
    assert {(e['source'], e['target'], e['type']) for e in edges} == {
        ('input', 'transport', 'pipe_flow'), ('transport', 'delivery', 'pipe_flow'),
        ('delivery', 'analytics', 'firehose_delivery'), ('delivery', 'errors', 'firehose_backup'),
        ('Data', 'analytics', 'table_storage')}
    assert not d.issues
    assert len(nodes) == 6
    clients['pipes'].describe_pipe.assert_called_once_with(Name='transport')
    clients['firehose'].describe_delivery_stream.assert_called_once_with(DeliveryStreamName='delivery')


@pytest.mark.parametrize('mode', ['denied', 'catalog', 'destination', 'more'])
def test_incomplete_enrichment_is_reported(mode):
    d, session, clients = subject()
    description = clients['firehose'].describe_delivery_stream.return_value['DeliveryStreamDescription']
    if mode == 'denied':
        clients['pipes'].describe_pipe.side_effect = RuntimeError('AccessDenied')
    elif mode == 'catalog':
        description['Destinations'][0]['IcebergDestinationDescription']['CatalogConfiguration'] = {}
    elif mode == 'destination':
        description['Destinations'] = [{'RedshiftDestinationDescription': {}}]
    else:
        description['HasMoreDestinations'] = True
    enrich(d, session)
    assert d.issues


@pytest.mark.parametrize('kind,arn', [
    ('AWS::Kinesis::Stream', A+'kinesis:eu-west-1:123456789012:stream/events'),
    ('AWS::Events::EventBus', A+'events:eu-west-1:123456789012:event-bus/events')])
def test_eventbridge_target_and_unknown_targets_are_accounted(kind, arn):
    d, _, clients = subject()
    d.component_resources = {'arbitrary-component': [
        {'type': 'AWS::Events::Rule', 'physical_id': 'bus|routing', 'logical_id': 'Routing'},
        {'type': kind, 'physical_id': arn, 'logical_id': 'Destination'}]}
    clients['events'].get_paginator.return_value.paginate.return_value = [
        {'Targets': [{'Arn': arn}, {'Arn': 'arn:aws:unknown:r:a:thing'}]}]
    d._enrich_eventbridge_targets()
    d.to_inventory_nodes()
    edges = d.to_edges()
    assert any(e['source'] == 'routing' and e['target'] == 'events' for e in edges)
    assert any(i['stage'] == 'unsupported_eventbridge_target' for i in d.issues)


def test_external_dataflow_remains_explicit():
    d, _, _ = subject()
    add_link(d, 'AWS::Events::Rule', 'routing', 'AWS::Events::EventBus',
             A+'events:eu-west-1:999999999999:event-bus/external', 'eventbridge_trigger', 'triggers', 'configuration')
    edges = list(resolved_links(d))
    assert edges[0]['target'].endswith('/external')
    assert any(i['stage'] == 'unresolved_dataflow_endpoint' for i in d.issues)


def test_default_view_hides_backup_not_application_bucket_and_preserves_graph(tmp_path):
    d, session, _ = subject()
    enrich(d, session)
    nodes = d.to_inventory_nodes()
    component = Component('unrelated-service', 'local')
    for n in nodes:
        component.add_node('storage' if n.resource_type == 's3' else 'queues' if n.resource_type == 'sqs' else 'misc', n)
    edges = [Edge(e['source'], e['target'], e['type'], e['label'], e['evidence']) for e in resolved_links(d)]
    before = component.to_dict()
    view, _, report = prepare_view(component, edges, ViewConfig(), EdgeFilterConfig(), D2Generator._safe_id)
    statuses = {r['name']: r['status'] for r in report['resources']}
    assert statuses['errors'] == 'excluded_purpose'
    assert statuses['analytics'] == 'included'
    assert component.to_dict() == before
    _, _, operational = prepare_view(component, edges, ViewConfig(excluded_purposes=[]), EdgeFilterConfig(), D2Generator._safe_id)
    assert next(r for r in operational['resources'] if r['name'] == 'errors')['status'] == 'included'
    templates = Path(__file__).parents[1]/'pndocgen/engine/d2/templates'
    g = D2Generator(templates)
    source = g.generate_l3(component, tmp_path/'semantic.d2', edges, pattern='lambda_microservice', detail_level='detailed')
    assert '8c6245d3da45607ec7a3ba8da27c4bb88bdf023ff34ebc257cd2c0c1cc129193.png' in source.read_text()
    assert g.render(source).is_file()


def test_global_purpose_rules_are_explicit_and_order_independent():
    rule = {'resource_type': 'sqs', 'name_prefix': 'acceptance-', 'purpose': 'test'}
    policy = ViewConfig.from_mapping({'purpose_rules': [rule]})
    assert policy.purpose_rules[0].matches(InventoryNode('id', 'acceptance-jobs', 'sqs', '', ''))
    assert 'test' in policy.excluded_purposes
    with pytest.raises(ValueError):
        ViewConfig.from_mapping({'purpose_rules': [{**rule, 'purpose': 'unknown'}]})


def test_named_bus_resolves_only_in_captured_account_region():
    d, _, _ = subject()
    d.dataflow_scopes.add(('aws', 'eu-west-1', '123456789012'))
    d.component_resources = {'component': [{'type': 'AWS::Events::EventBus', 'physical_id': 'events', 'logical_id': 'Bus'}]}
    d.to_inventory_nodes()
    for account in ('123456789012', '999999999999'):
        add_link(d, 'AWS::Events::Rule', 'rule', 'AWS::Events::EventBus',
                 f'arn:aws:events:eu-west-1:{account}:event-bus/events', 'eventbridge_trigger', 'triggers', 'configuration')
    links = list(resolved_links(d))
    assert links[0]['target'] == 'events'
    assert links[1]['target'] == 'arn:aws:events:eu-west-1:999999999999:event-bus/events'


def test_backup_also_used_as_primary_is_kept():
    c = Component('arbitrary', 'local')
    c.add_node('storage', InventoryNode('id', 'shared', 's3', 'local', 'local'))
    edges = [Edge('stream', 'shared', kind) for kind in ('firehose_backup', 'firehose_delivery')]
    _, _, report = prepare_view(c, edges, ViewConfig(), EdgeFilterConfig(), D2Generator._safe_id)
    assert report['resources'][0]['status'] == 'included'
