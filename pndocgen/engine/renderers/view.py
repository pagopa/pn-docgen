"""Prepare a deterministic view and report scope decisions without mutating the IR."""
from copy import deepcopy
from pndocgen.engine.core.models import _cluster_key
from pndocgen.engine.core.models import get_resource_meta, ResourceKind, SemanticRole
from pndocgen.engine.core.config import get_app_config


def prepare_view(component, edges, policy, edge_filter, safe_id):
    view = deepcopy(component)
    overrides = get_app_config().resource_kind_overrides
    # Reclassify legacy saved graphs as well as freshly resolved inventories.
    moves = []
    for cluster in view.clusters.values():
        for node in list(cluster.nodes):
            target = ("rules" if node.resource_type == "aws_scheduler_schedule" else
                      "security" if node.resource_type == "aws_apigateway_authorizer" else None)
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
    for cluster_name, cluster in view.clusters.items():
        kept = []
        for node in cluster.nodes:
            matched = [r for r in policy.exclusions if r.matches(component.name, node)]
            meta = get_resource_meta(node.resource_type, overrides)
            hidden_type = meta.kind != ResourceKind.NODE
            hidden_auxiliary = cluster_name in policy.excluded_clusters
            if matched or hidden_type or hidden_auxiliary:
                omitted.add(node.name)
            else:
                kept.append(node)
            resources.append({"name": node.name, "type": node.resource_type, "cluster": cluster_name,
                              "status": "excluded" if matched else "excluded_resource_kind" if hidden_type else "excluded_auxiliary_cluster" if hidden_auxiliary else "included",
                              "reason": "configured name prefix" if matched else
                              "central resource classification: " + meta.kind.value if hidden_type else
                              f"essential view omits {'/'.join(policy.excluded_clusters)}; retained in source graph" if hidden_auxiliary else
                              "unclassified type: retained as fallback" if meta.role == SemanticRole.UNKNOWN else
                              "in component scope"})
        cluster.nodes = kept
    selected = []
    relations = []
    cluster_of = {n.name: c for c, cluster in component.clusters.items() for n in cluster.nodes}
    def edge_key(e):
        return (_cluster_key(cluster_of.get(e.source, "")),
                _cluster_key(cluster_of.get(e.target, "")),
                e.source.encode(), e.target.encode(), e.edge_type.encode(), (e.label or "").encode(),
                (e.evidence or "").encode())
    for edge in sorted(edges, key=edge_key):
        matched = next((rule for rule in policy.edge_exclusions
                        if rule.matches(component.name, edge)), None)
        if edge.source in omitted or edge.target in omitted:
            status = "excluded_endpoint"
        elif edge.source not in names or edge.target not in names:
            status = "endpoint_outside_component"
        elif not edge_filter.is_allowed(edge.edge_type):
            status = "excluded_edge_type"
        elif matched is not None:
            status = "excluded_edge_rule"
        else:
            status = "eligible"
            selected.append(edge)
        relation = {**edge.to_dict(), "status": status}
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
    identities = {(r["source"], r["target"], r["type"]) for r in eligible}
    seen = set()
    for relation in eligible:
        source, target, kind = relation["source"], relation["target"], relation["type"]
        src, dst = lookup[source], lookup[target]
        identity = (source, target, kind)
        if simplified and (pattern == 'ecs_microservice' or src[0] != dst[0]):
            pair = (src[0], dst[0])
            if src[0] == dst[0]:
                relation["status"] = "omitted_intra_cluster_simplified"
                continue
            status = "aggregated_cluster_pair"
        else:
            pair = (".".join(src), ".".join(dst))
            status = "represented_node_edge"
            if pattern == "ecs_microservice" and kind == "sqs_producer" and (target, source, "sqs_consumer") in identities:
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
