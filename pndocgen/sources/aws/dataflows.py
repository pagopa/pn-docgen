"""Read-only enrichment of explicit dataflow configuration, independent of names."""
import re


def arn_type(arn):
    if not isinstance(arn, str):
        return None
    parts = arn.split(':', 5)
    if len(parts) != 6 or parts[0] != 'arn':
        return None
    service, resource = parts[2], parts[5]
    return {'kinesis': 'AWS::Kinesis::Stream', 'firehose': 'AWS::KinesisFirehose::DeliveryStream',
            'sqs': 'AWS::SQS::Queue', 'sns': 'AWS::SNS::Topic',
            'lambda': 'AWS::Lambda::Function', 's3': 'AWS::S3::Bucket',
            'states': 'AWS::StepFunctions::StateMachine', 'pipes': 'AWS::Pipes::Pipe',
            'events': 'AWS::Events::EventBus' if resource.startswith('event-bus/') else None,
            's3tables': 'AWS::S3Tables::Table' if '/table/' in resource else 'AWS::S3Tables::TableBucket',
            }.get(service)


def add_link(discoverer, source_type, source, target_type, target, kind, label, evidence):
    if not source or not source_type or not target or not target_type:
        discoverer._record_issue('unresolved_dataflow', source or source_type,
                                 f'{kind}: missing or unsupported ' +
                                 (f'source {source!r}' if not source or not source_type else f'target {target!r}'))
        return
    entry = dict(source_type=source_type, source=source, target_type=target_type,
                 target=target, type=kind, label=label, evidence=evidence)
    if entry not in discoverer.dataflow_links:
        discoverer.dataflow_links.append(entry)


def enrich(discoverer, session):
    """Only describe resources present in the selected component's CFN records."""
    resources = {(r['type'], r['physical_id']) for rows in discoverer.component_resources.values()
                 for r in rows}
    clients = {}
    def client(service):
        if service not in clients:
            clients[service] = session.client(service, region_name=discoverer.region)
        return clients[service]
    for kind, identifier in sorted(resources):
        try:
            if kind == 'AWS::Pipes::Pipe':
                response = client('pipes').describe_pipe(Name=identifier.rsplit('/', 1)[-1])
                discoverer.dataflow_descriptions[identifier] = response
                source = response.get('Source')
                target = response.get('Target')
                stream = re.fullmatch(r'(arn:[^:]+:dynamodb:[^:]+:[^:]+:table/[^/]+)/stream/[^/]+', source or '')
                add_link(discoverer, 'AWS::DynamoDB::Table' if stream else arn_type(source),
                         stream.group(1) if stream else source, kind, identifier,
                         'dynamodb_stream' if stream else 'pipe_flow',
                         'stream' if stream else 'forwards', 'pipe_configuration')
                enrichment = response.get('Enrichment')
                if enrichment:
                    add_link(discoverer, kind, identifier, arn_type(enrichment), enrichment,
                             'pipe_enrichment', 'enriches', 'pipe_configuration')
                add_link(discoverer, kind, identifier, arn_type(target), target,
                         'pipe_flow', 'forwards', 'pipe_configuration')
            elif kind == 'AWS::KinesisFirehose::DeliveryStream':
                response = client('firehose').describe_delivery_stream(
                    DeliveryStreamName=identifier.rsplit('/', 1)[-1])['DeliveryStreamDescription']
                discoverer.dataflow_descriptions[identifier] = response
                source = response.get('Source', {}).get('KinesisStreamSourceDescription', {}).get('KinesisStreamARN')
                if source:
                    add_link(discoverer, arn_type(source), source, kind, identifier,
                             'firehose_delivery', 'delivers', 'firehose_configuration')
                destinations = response.get('Destinations', [])
                if not destinations:
                    discoverer._record_issue('unresolved_dataflow', identifier, 'Firehose has no described destinations')
                for destination in destinations:
                    iceberg = destination.get('IcebergDestinationDescription')
                    if iceberg is not None:
                        catalog = iceberg.get('CatalogConfiguration', {}).get('CatalogARN', '')
                        match = re.fullmatch(r'arn:([^:]+):glue:([^:]+):([^:]+):catalog/s3tablescatalog/([^/]+)', catalog)
                        if match:
                            partition, region, account, bucket = match.groups()
                            target = f'arn:{partition}:s3tables:{region}:{account}:bucket/{bucket}'
                            add_link(discoverer, kind, identifier, 'AWS::S3Tables::TableBucket', target,
                                     'firehose_delivery', 'delivers', 'firehose_catalog_configuration')
                        else:
                            discoverer._record_issue('unsupported_firehose_catalog', identifier, catalog or 'Missing catalog ARN')
                        backup = iceberg.get('S3DestinationDescription', {}).get('BucketARN')
                        if backup:
                            add_link(discoverer, kind, identifier, 'AWS::S3::Bucket', backup,
                                     'firehose_backup', 'backs up', 'firehose_configuration')
                    else:
                        primary = destination.get('ExtendedS3DestinationDescription') or destination.get('S3DestinationDescription')
                        if primary:
                            add_link(discoverer, kind, identifier, 'AWS::S3::Bucket', primary.get('BucketARN'),
                                     'firehose_delivery', 'delivers', 'firehose_configuration')
                            backup = primary.get('S3BackupDescription', {}).get('BucketARN')
                            if backup:
                                add_link(discoverer, kind, identifier, 'AWS::S3::Bucket', backup,
                                         'firehose_backup', 'backs up', 'firehose_configuration')
                        else:
                            discoverer._record_issue('unsupported_firehose_destination', identifier,
                                                     ', '.join(sorted(destination)))
                if response.get('HasMoreDestinations'):
                    discoverer._record_issue('incomplete_firehose_destinations', identifier, 'Additional destinations not captured')
            elif kind == 'AWS::S3Tables::Table':
                if '/table/' in identifier:
                    add_link(discoverer, kind, identifier, 'AWS::S3Tables::TableBucket', identifier.split('/table/')[0],
                             'table_storage', 'stored in', 'resource_arn_identity')
                else:
                    discoverer._record_issue('unresolved_dataflow', identifier, 'Unrecognized S3 Tables table identity')
        except Exception as exc:
            discoverer._record_issue('describe_dataflow', identifier, exc)


def resolved_links(discoverer):
    """Join by typed physical identity; unresolved references stay explicit."""
    for link in discoverer.dataflow_links:
        endpoints = {}
        for endpoint in ('source', 'target'):
            kind, identifier = link[endpoint + '_type'], link[endpoint]
            aliases = [identifier]
            parts = identifier.split(':', 5)
            if len(parts) == 6 and parts[0] == 'arn':
                if kind == 'AWS::S3::Bucket':
                    aliases.append(parts[5])
                elif kind == 'AWS::SQS::Queue':
                    domain = 'amazonaws.com.cn' if parts[1] == 'aws-cn' else 'amazonaws.com'
                    aliases.append(f'https://sqs.{parts[3]}.{domain}/{parts[4]}/{parts[5]}')
                elif (kind in {'AWS::Events::EventBus', 'AWS::Kinesis::Stream', 'AWS::KinesisFirehose::DeliveryStream', 'AWS::DynamoDB::Table'}
                      and (parts[1], parts[3], parts[4]) in discoverer.dataflow_scopes):
                    aliases.append(parts[5].split('/', 1)[-1])
            matches = set().union(*(discoverer._resource_aliases.get((kind, value), set()) for value in aliases))
            if len(matches) > 1:
                raise ValueError(f'Ambiguous dataflow endpoint: {kind} {identifier}')
            endpoints[endpoint] = next(iter(matches), identifier)
            if not matches:
                discoverer._record_issue('unresolved_dataflow_endpoint', identifier,
                                         f'{link["type"]}: {endpoint} outside captured resources or unresolved')
        yield {**endpoints, **{k: link[k] for k in ('type', 'label', 'evidence')}}
