"""Prepare a deterministic view and report scope decisions without mutating the IR."""
from copy import deepcopy
from dataclasses import replace
from pndocgen.engine.core.models import _cluster_key
from pndocgen.engine.core.models import get_resource_meta, ResourceKind, SemanticRole
from pndocgen.engine.core.config import get_app_config
from pndocgen.engine.core.evidence import evidence_class, bidirectional_pairs
import json


def prepare_view(component, edges, policy, edge_filter, safe_id, *, resolution_scope=None):
    resolution_scope = resolution_scope or {}
    view = deepcopy(component)
    overrides = get_app_config().resource_kind_overrides
    # Reclassify legacy saved graphs as well as freshly resolved inventories.
    moves = []
    for cluster in view.clusters.values():
        for node in list(cluster.nodes):
            target = {'aws_s3tables_tablebucket': 'storage', 'aws_s3tables_table': 'storage',
                       'aws_kinesisfirehose_deliverystream': 'queues', 'eventbridge_pipe': 'queues',
                       'aws_events_eventbus': 'rules', 'aws_scheduler_schedule': 'rules',
                       'aws_apigateway_authorizer': 'security'}.get(node.resource_type)
            if target and cluster.name != target:
                cluster.nodes.remove(node)
                moves.append((target, node))
    for target, node in moves:
        view.add_node(target, node)
    view.canonicalize()
    names = set()
    paths = set()
    # Reject ambiguous joins before filtering: exclusions must not conceal invalid IR.
    for cluster_name, cluster in view.clusters.items():
        for node in cluster.nodes:
            if node.name in names:
                raise ValueError(f"Ambiguous resource name in {component.name}: {node.name}")
            names.add(node.name)
            path = (cluster_name, safe_id(node.name))
            if path in paths:
                raise ValueError(f"D2 identifier collision in {component.name}: {path}")
            paths.add(path)
    omitted = set()
    resources = []
    backups = {e.target for e in edges if e.edge_type == 'firehose_backup'}
    primary_destinations = {e.target for e in edges if e.edge_type == 'firehose_delivery'}
    for cluster_name, cluster in view.clusters.items():
        kept = []
        for node in cluster.nodes:
            matched = [r for r in policy.exclusions if r.matches(component.name, node)]
            meta = get_resource_meta(node.resource_type, overrides)
            hidden_type = meta.kind != ResourceKind.NODE
            hidden_auxiliary = cluster_name in policy.excluded_clusters
            purposes = {rule.purpose for rule in policy.purpose_rules if rule.matches(node)}
            if len(purposes) > 1:
                raise ValueError(f'Conflicting purpose rules for {node.name}: {sorted(purposes)}')
            inferred = 'logging' if node.name in backups - primary_destinations else 'application'
            purpose = next(iter(purposes), inferred)
            hidden_purpose = purpose in policy.excluded_purposes
            hidden_reference = (node.metadata.get('discovered_from') == 'configured_reference'
                                and policy.external_references == 'none')
            if matched or hidden_type or hidden_auxiliary or hidden_purpose or hidden_reference:
                omitted.add(node.name)
            else:
                kept.append(node)
            resources.append({"name": node.name, "type": node.resource_type, "cluster": cluster_name,
                              "status": "excluded" if matched else "excluded_resource_kind" if hidden_type else "excluded_auxiliary_cluster" if hidden_auxiliary else "excluded_purpose" if hidden_purpose else "external_reference_omitted" if hidden_reference else "included",
                              "reason": "configured name prefix" if matched else
                              "central resource classification: " + meta.kind.value if hidden_type else
                              f"essential view omits {'/'.join(policy.excluded_clusters)}; retained in source graph" if hidden_auxiliary else
                              f"configured excluded purpose: {purpose}" if hidden_purpose else
                              "external_references: none; declaration retained in graph" if hidden_reference else
                              "unclassified type: retained as fallback" if meta.role == SemanticRole.UNKNOWN else
                              "configured external reference; not independently discovered"
                              if node.metadata.get('external_reference') else "in component scope"})
            for context_key in ('eventbridge_rule', 'eventbridge_pipe'):
                if context_key in node.metadata:
                    resources[-1][context_key] = deepcopy(node.metadata[context_key])
            if purpose != 'application' or purposes:
                resources[-1]['purpose'] = purpose
                resources[-1]['purpose_evidence'] = ('configured rule' if purposes else
                    'firehose backup destination' if purpose == 'logging' else 'resource type default; configurable')
        cluster.nodes = kept
    # Presentation-only aggregation: one visible S3 Tables resource per bucket.
    types = {n.name: n.resource_type for c in component.clusters.values() for n in c.nodes}
    owners = {}
    for edge in edges:
        if (edge.edge_type == 'table_storage' and types.get(edge.source) == 'aws_s3tables_table'
                and types.get(edge.target) == 'aws_s3tables_tablebucket'):
            owners.setdefault(edge.source, set()).add(edge.target)
    projected = {table: next(iter(buckets)) for table, buckets in owners.items() if len(buckets) == 1}
    tables = {name for name, kind in types.items() if kind == 'aws_s3tables_table'}
    for cluster in view.clusters.values():
        cluster.nodes = [node for node in cluster.nodes if node.name not in tables]
    for resource in resources:
        if resource['name'] in tables and resource['status'] == 'included':
            resource['status'] = 'aggregated_into_table_bucket' if resource['name'] in projected else 'unresolved_table_bucket'
            resource['reason'] = 'S3 Tables internals retained in inventory, not rendered separately'
            if resource['name'] in projected:
                resource['table_bucket'] = projected[resource['name']]
    selected = []
    relations = []
    cluster_of = {n.name: c for c, cluster in component.clusters.items() for n in cluster.nodes}
    metadata_of = {n.name: n.metadata for cluster in component.clusters.values() for n in cluster.nodes}
    def edge_key(e):
        return (_cluster_key(cluster_of.get(e.source, "")),
                _cluster_key(cluster_of.get(e.target, "")),
                e.source.encode(), e.target.encode(), e.edge_type.encode(), (e.label or "").encode(),
                (e.evidence or "").encode(), json.dumps(e.source_ref, sort_keys=True),
                json.dumps(e.target_ref, sort_keys=True), json.dumps(e.evidence_details, sort_keys=True))
    for edge in sorted(edges, key=edge_key):
        rendered = replace(edge, source=projected.get(edge.source, edge.source),
                           target=projected.get(edge.target, edge.target))
        if edge.edge_type == 'eventbridge_rule_association':
            pattern = metadata_of.get(edge.target, {}).get('eventbridge_rule', {}).get('EventPattern')
            if pattern:
                detail = pattern.get('detail-type') if isinstance(pattern, dict) else None
                simple = (isinstance(pattern, dict) and set(pattern) == {'detail-type'}
                          and isinstance(detail, list) and len(detail) == 1
                          and isinstance(detail[0], str) and 0 < len(detail[0]) <= 72
                          and '\n' not in detail[0] and not detail[0].startswith('arn:'))
                rendered = replace(rendered, label='matches ' + detail[0] if simple else 'filters events')
        matched = next((rule for rule in policy.edge_exclusions
                        if rule.matches(component.name, edge)), None)
        affected = {edge.source, edge.target, rendered.source, rendered.target} & omitted
        explicit_omissions = {r['name'] for r in resources
                              if r['status'] not in {'included', 'external_reference_omitted'}}
        if affected:
            status = ("excluded_endpoint" if affected & explicit_omissions else "external_reference_omitted")
        elif edge.edge_type == 'table_storage' and edge.source in projected:
            status = 'aggregated_containment'
        elif rendered != edge and rendered.source == rendered.target:
            status = 'aggregated_internal_relation'
        elif any(n in tables and n not in projected for n in (edge.source, edge.target)):
            status = 'unresolved_table_bucket'
        elif edge.source not in names or edge.target not in names:
            status = "endpoint_outside_component"
        elif not edge_filter.is_allowed(edge.edge_type):
            status = "excluded_edge_type"
        elif matched is not None:
            status = "excluded_edge_rule"
        else:
            status = "eligible"
            selected.append(rendered)
        relation = {**edge.to_dict(), "status": status}
        relation["evidence_class"] = evidence_class(edge.evidence)
        diagnostics = {}
        for endpoint in ('source', 'target'):
            name = getattr(edge, endpoint)
            if name in omitted:
                resource = next(r for r in resources if r['name'] == name)
                diagnostics[endpoint] = {'reason': resource['status']}
            elif name not in names:
                from pndocgen.sources.aws.configured_references import SUPPORTED, concrete_identity
                ref = getattr(edge, endpoint + '_ref')
                reason = ('identity_unresolved' if not ref else
                          'unsupported_external_reference_type' if ref['type'] not in SUPPORTED else
                          'external_reference_not_captured' if concrete_identity(ref) else 'identity_unresolved')
                if (ref and ref['type'] == 'AWS::SQS::Queue'
                        and resolution_scope.get('excluded_external_queues') is True):
                    reason = 'excluded_external_queues'
                diagnostics[endpoint] = {'reason': reason, **({'reference': ref} if ref else {})}
        if diagnostics:
            relation['endpoint_diagnostics'] = diagnostics
        if rendered != edge and status == 'eligible':
            relation['view_source'] = rendered.source
            relation['view_target'] = rendered.target
            if rendered.label != edge.label:
                relation['view_label'] = rendered.label
        if status == "excluded_edge_rule":
            relation["matched_rule"] = matched.to_dict()
        relations.append(relation)
    report = {"version": 1, "component": component.name, "resources": resources,
              "relationships": relations,
              "note": "eligible means selected before rendering/aggregation, not proof of runtime traffic"}
    return view, selected, report


def account_rendered_edges(report, lookup, pattern, detail, emitted):
    """Account for node edges, cluster aggregation and SQS bidirectional collapse.

    References are to pre-ingress D2 paths: the ingress adapter only adds scope.
    This traces model-to-D2 decisions, not the truth of an inferred AWS relation.
    """
    simplified = detail == "simplified"
    pairs = {(e.get("from_box", e.get("from_path")), e.get("to_box", e.get("to_path")))
             for e in emitted}
    eligible = [r for r in report["relationships"] if r["status"] == "eligible"]
    from pndocgen.engine.core.models import Edge
    projected_edges = [Edge(r.get('view_source', r['source']), r.get('view_target', r['target']),
                            r['type'], r.get('label', ''), r.get('evidence', ''),
                            evidence_details=r.get('evidence_details', {})) for r in eligible]
    collapsed = bidirectional_pairs(projected_edges,
        legacy=get_app_config().render.view.evidence_labels == 'legacy')
    seen = set()
    for relation in eligible:
        source, target, kind = relation.get("view_source", relation["source"]), relation.get("view_target", relation["target"]), relation["type"]
        src, dst = lookup[source], lookup[target]
        identity = (source, target, kind)
        if get_app_config().render.view.evidence_labels != 'legacy':
            identity += (relation.get('label', ''), relation.get('evidence', ''),
                         json.dumps(relation.get('evidence_details', {}), sort_keys=True))
        if simplified and (pattern == 'ecs_microservice' or src[0] != dst[0]):
            pair = (src[0], dst[0])
            if src[0] == dst[0]:
                relation["status"] = "omitted_intra_cluster_simplified"
                continue
            status = "aggregated_cluster_pair"
        else:
            pair = (".".join(src), ".".join(dst))
            status = "represented_node_edge"
            if pattern == "ecs_microservice" and kind == "sqs_producer" and (target, source) in collapsed:
                pair = (pair[1], pair[0])
                status = "collapsed_bidirectional_sqs"
            elif identity in seen:
                status = "deduplicated_same_endpoints_and_type"
        seen.add(identity)
        if pair not in pairs:
            raise ValueError(f"Unaccounted rendered relation: {source} -> {target} ({kind})")
        relation.update(status=status, d2_endpoints=list(pair))
    report["note"] = ("Relationships traced to pre-ingress D2 paths; cluster aggregation and duplicate "
                      "labels may lose detail. IAM-derived permissions do not prove runtime traffic.")
