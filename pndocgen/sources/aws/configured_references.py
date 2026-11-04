"""Materialize only concrete, one-hop configured references. No AWS requests."""
import re

from pndocgen.engine.core.models import InventoryNode, ResourceKind, get_resource_meta
from pndocgen.sources.aws.resource_identity import sqs_identity

SUPPORTED = {
    "AWS::SQS::Queue": ("sqs", "queues"),
    "AWS::Events::EventBus": ("aws_events_eventbus", "rules"),
}
CONFIGURED_EVIDENCE = frozenset({
    "pipe_configuration", "eventbridge_target_configuration", "eventbridge_rule_configuration",
    "event_source_mapping_configuration", "sns_subscription_configuration",
})
_BUS = re.compile(r"arn:aws(?:-cn|-us-gov)?:events:[a-z0-9-]+:\d{12}:event-bus/[A-Za-z0-9._/-]+")


def concrete_identity(ref):
    if not isinstance(ref, dict):
        return None
    kind, identifier = ref.get("type"), ref.get("id")
    if kind == "AWS::SQS::Queue":
        return sqs_identity(identifier)
    if kind == "AWS::Events::EventBus" and isinstance(identifier, str) and _BUS.fullmatch(identifier):
        return identifier
    return None


def node_identity(node):
    return (concrete_identity(node.metadata.get("resource_identity")) or
            (sqs_identity(node.id) if node.resource_type == "sqs" else node.id))


def materialize(graph, relationships, resolver, *, include_external_queues=True):
    """Keep reference nodes in the IR; the view's none/configured policy selects them.

    Ownership is taken only from the original graph, never from newly added
    references. A declaration cannot recursively grow the component perimeter.
    """
    original = {name: [n for c in comp.clusters.values() for n in c.nodes]
                for name, comp in graph.components.items()}
    owned_names = {}
    for nodes in original.values():
        for node in nodes:
            identity = node_identity(node)
            if identity:
                owned_names.setdefault((node.resource_type, identity), set()).add(node.name)
    requests = {}
    for relation in relationships:
        if relation.get("evidence") not in CONFIGURED_EVIDENCE:
            continue
        for endpoint, other in (("source", "target"), ("target", "source")):
            ref = relation.get(endpoint + "_ref")
            identity = concrete_identity(ref)
            if not identity:
                continue
            kind, cluster = SUPPORTED[ref["type"]]
            for comp_name, nodes in original.items():
                if not any(n.name == relation.get(other) for n in nodes):
                    continue
                if any(node_identity(n) == identity
                       and n.resource_type == kind for n in nodes):
                    continue
                requests[(comp_name, identity)] = (ref, kind, cluster)
    for (comp_name, identity), (ref, kind, cluster) in sorted(requests.items()):
        comp = graph.components[comp_name]
        nodes = [n for c in comp.clusters.values() for n in c.nodes]
        if kind == "sqs" and not include_external_queues:
            continue
        if any(node_identity(n) == identity
               and n.resource_type == kind for n in nodes):
            continue
        names = owned_names.get((kind, identity), set())
        if len(names) > 1:
            raise ValueError(f"Ambiguous configured identity: {identity}")
        name = next(iter(names)) if names else (
            identity.rsplit(":", 1)[-1] if kind == "sqs" else identity.split(":event-bus/", 1)[1])
        if any(n.name == name for n in nodes):
            raise ValueError(f"Ambiguous configured reference name: {name}")
        node = InventoryNode(identity, name, kind, identity.split(":")[4], identity.split(":")[3],
                             metadata={"resource_identity": dict(ref, id=identity),
                                       "external_reference": True,
                                       "discovered_from": "configured_reference"})
        if resolver._is_excluded(node) or get_resource_meta(kind, resolver.meta_overrides).kind != ResourceKind.NODE:
            continue
        comp.add_node(cluster, node)
    # Project all evidence (including IAM) only onto exact identities now present.
    known = {}
    for comp in graph.components.values():
        for cluster in comp.clusters.values():
            for node in cluster.nodes:
                identity = node_identity(node)
                if identity:
                    known.setdefault((node.resource_type, identity), set()).add(node.name)
    for relation in relationships:
        for endpoint in ("source", "target"):
            ref = relation.get(endpoint + "_ref")
            identity = concrete_identity(ref)
            if identity:
                kind = SUPPORTED[ref["type"]][0]
                names = known.get((kind, identity), set())
                if len(names) > 1:
                    raise ValueError(f"Ambiguous configured identity: {identity}")
                relation[endpoint] = next(iter(names), identity)
