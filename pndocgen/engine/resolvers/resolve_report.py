"""Account for every inventory input before the rendering view is selected."""
from collections import Counter
from pndocgen.engine.core.models import ResourceKind, get_resource_meta


def resolution_report(nodes, relationships, graph, resolver):
    included = {(n.id, n.name, n.resource_type) for comp in graph.components.values()
                for cluster in comp.clusters.values() for n in cluster.nodes}
    orphans = {(n.id, n.name, n.resource_type) for n in graph.orphans}
    resources = []
    for node in sorted(nodes, key=lambda n: (n.name, n.resource_type, n.id)):
        identity = (node.id, node.name, node.resource_type)
        kind = get_resource_meta(node.resource_type, resolver.meta_overrides).kind
        if identity in included:
            status = "included_in_graph"
        elif resolver._is_excluded(node):
            status = "excluded_name_pattern"
        elif kind == ResourceKind.SKIP:
            status = "excluded_resource_kind"
        elif kind == ResourceKind.EDGE:
            endpoints = (node.metadata.get("source"), node.metadata.get("target"))
            status = ("converted_to_relationship" if any((e.source, e.target) == endpoints for e in graph.edges)
                      else "relationship_resource_not_converted_by_resolver")
        elif identity in orphans:
            status = "orphan_without_component"
        else:
            raise ValueError(f"Unaccounted inventory resource: {node.id}")
        resources.append({"id": node.id, "name": node.name, "type": node.resource_type, "status": status})

    def identity(edge):
        return (edge["source"], edge["target"], edge["type"], edge.get("label", ""), edge.get("evidence", ""))
    counts = Counter(identity(e) for e in relationships)
    resolved = {identity(e.to_dict()) for e in graph.edges}
    edges = []
    for relation in sorted(relationships, key=identity):
        key = identity(relation)
        if key not in resolved:
            raise ValueError(f"Unaccounted inventory relationship: {key}")
        edges.append({**relation, "status": "coalesced_exact_duplicate" if counts[key] > 1 else "preserved_in_graph"})
    original_ids = {(n.id, n.name, n.resource_type) for n in nodes}
    additions = [{"id": n.id, "name": n.name, "type": n.resource_type, "component": comp.name,
                  "reason": n.metadata.get("discovered_from", "resolver_enrichment")}
                 for comp in graph.components.values() for cluster in comp.clusters.values() for n in cluster.nodes
                 if (n.id, n.name, n.resource_type) not in original_ids]
    return {"version": 1, "resources": resources, "relationships": edges, "added_resources": additions,
            "note": "Preserved in graph does not imply represented in a specific view; inspect its .view.json report."}
