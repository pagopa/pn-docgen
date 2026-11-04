"""Pure reconciliation of concrete AWS resource references; no AWS clients."""
import re


_QUEUE = r'(?P<name>[A-Za-z0-9_-]+(?:\.fifo)?)'
_ARN = re.compile(r'arn:(?P<partition>aws(?:-cn|-us-gov)?):sqs:'
                  r'(?P<region>[a-z0-9-]+):(?P<account>\d{12}):' + _QUEUE)
_URL = re.compile(r'https://sqs\.(?P<region>[a-z0-9-]+)\.'
                  r'(?P<domain>amazonaws\.com(?:\.cn)?)/(?P<account>\d{12})/' + _QUEUE)


def sqs_identity(identifier):
    """Return a canonical ARN only when the identifier includes its full scope."""
    if not isinstance(identifier, str):
        return None
    match = _ARN.fullmatch(identifier)
    if match:
        return identifier
    match = _URL.fullmatch(identifier)
    if not match:
        return None
    fields = match.groupdict()
    region = fields['region']
    partition = 'aws-cn' if region.startswith('cn-') else 'aws-us-gov' if region.startswith('us-gov-') else 'aws'
    if fields['domain'] != ('amazonaws.com.cn' if partition == 'aws-cn' else 'amazonaws.com'):
        return None
    return f'arn:{partition}:sqs:{region}:{fields["account"]}:{fields["name"]}'


def sqs_reference(identifier):
    identity = sqs_identity(identifier)
    return {'type': 'AWS::SQS::Queue', 'id': identity} if identity else None


def reconcile_sqs(graph, relationships):
    """Project typed queue references onto existing nodes, never onto a namesake.

    Unresolved concrete references keep the ARN so the view cannot accidentally
    join them to a same-named queue in another account or region. Legacy names
    without a typed reference are left unchanged.
    """
    from pndocgen.engine.core.models import endpoint_reference
    known = {}
    for component in graph.components.values():
        for cluster in component.clusters.values():
            for node in cluster.nodes:
                identity = sqs_identity(node.id) if node.resource_type == 'sqs' else None
                if identity:
                    known.setdefault(identity, set()).add(node.name)
    for relation in relationships:
        for endpoint in ('source', 'target'):
            ref = endpoint_reference(relation.get(endpoint + '_ref'))
            if not ref or ref['type'] != 'AWS::SQS::Queue':
                continue
            identity = sqs_identity(ref['id'])
            if not identity:
                raise ValueError(f'Invalid concrete SQS identity reference: {ref!r}')
            relation[endpoint + '_ref'] = {**ref, 'id': identity}
            names = known.get(identity, set())
            if len(names) > 1:
                raise ValueError(f'Ambiguous SQS identity reference: {identity}')
            relation[endpoint] = next(iter(names), identity)
