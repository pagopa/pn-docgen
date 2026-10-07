from test_dataflows import subject, QUEUE
from pndocgen.sources.aws.dataflows import enrich, resolved_links

def test_pipe_projects_stream_to_owned_table_with_stream_semantics():
    d, session, clients = subject()
    d.dataflow_scopes.add(('aws','eu-west-1','123456789012'))
    d.component_resources['arbitrary-component'].append(
        {'type':'AWS::DynamoDB::Table','physical_id':'records','logical_id':'Records'})
    clients['pipes'].describe_pipe.return_value = {
        'Source':'arn:aws:dynamodb:eu-west-1:123456789012:table/records/stream/2026-01-01', 'Target':QUEUE}
    enrich(d, session)
    d.to_inventory_nodes()
    edges = list(resolved_links(d))
    assert any(e['source']=='records' and e['target']=='transport' and e['type']=='dynamodb_stream' for e in edges)
    assert not d.issues

def test_external_stream_does_not_join_same_named_local_table():
    d, session, clients = subject()
    d.dataflow_scopes.add(('aws','eu-west-1','123456789012'))
    d.component_resources['arbitrary-component'].append(
        {'type':'AWS::DynamoDB::Table','physical_id':'records','logical_id':'Records'})
    clients['pipes'].describe_pipe.return_value = {
        'Source':'arn:aws:dynamodb:eu-west-1:999999999999:table/records/stream/2026-01-01', 'Target':QUEUE}
    enrich(d, session)
    d.to_inventory_nodes()
    edges = list(resolved_links(d))
    assert any(e['source'].startswith('arn:aws:dynamodb:') for e in edges)
    assert any(i['stage']=='unresolved_dataflow_endpoint' for i in d.issues)
