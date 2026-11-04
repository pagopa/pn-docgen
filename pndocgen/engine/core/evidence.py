"""Presentation of evidence never upgrades configuration or permissions to traffic."""
import json


def evidence_class(value):
    if value in {'iam_permission', 'lambda_resource_policy_permission', 'sqs_resource_policy_permission'}:
        return 'permission'
    if value in {'pipe_configuration', 'eventbridge_target_configuration', 'eventbridge_rule_configuration',
                 'event_source_mapping_configuration', 'sns_subscription_configuration',
                 'firehose_configuration', 'firehose_catalog_configuration', 'scheduler_configuration',
                 'resource_arn_identity', 'cfn_template_target_reference'}:
        return 'configured'
    if value == 'environment_reference':
        return 'reference'
    if value == 'same_component_heuristic':
        return 'heuristic'
    if value == 'iam_or_environment_reference':
        return 'mixed'
    return 'unspecified'


def signature(edge):
    return (edge.evidence, json.dumps(edge.evidence_details, sort_keys=True, separators=(',', ':')))


def display_label(edge, fallback="", *, legacy=False, bidirectional=False):
    label = edge.label or fallback
    if edge.edge_type == 'sqs_producer' and label.casefold() == 'sends':
        label = 'produces'
    if legacy:
        return 'consumes/produces' if bidirectional else label
    category = evidence_class(edge.evidence)
    if category == 'permission':
        label = {'sqs_producer': 'send', 'sqs_consumer': 'receive',
                 'eventbridge_publish_permission': 'PutEvents'}.get(edge.edge_type, label)
        label = {'reads': 'read', 'writes': 'write', 'reads/writes': 'read/write'}.get(label, label)
        if bidirectional:
            label = 'send/receive'
        prefix = 'IAM' if edge.evidence == 'iam_permission' else 'permission'
    else:
        prefix = {'configured': 'config', 'reference': 'reference', 'heuristic': 'heuristic',
                  'mixed': 'mixed', 'unspecified': 'unspecified'}[category]
        if bidirectional:
            label = 'consumes/produces'
    conditional = ' (conditional)' if 'condition' in edge.evidence_details else ''
    return f'{prefix}: {label or "relation"}{conditional}'


def bidirectional_pairs(edges, *, legacy=False):
    pairs = set()
    for edge in edges:
        if edge.edge_type != 'sqs_consumer':
            continue
        reverse = [other for other in edges if other.edge_type == 'sqs_producer'
                   and other.source == edge.target and other.target == edge.source]
        same = [other for other in edges if other.edge_type == 'sqs_consumer'
                and other.source == edge.source and other.target == edge.target]
        if reverse and (legacy or len({signature(e) for e in reverse + same}) == 1):
            pairs.add((edge.source, edge.target))
    return pairs
