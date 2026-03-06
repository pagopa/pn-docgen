"""
pn-docgen - Tag Resolver

Reads pndocgen.yaml config and applies tagging rules to raw InventoryNodes,
producing a ComponentGraph (the structured intermediate format).

Logic:
  1. Load pndocgen.yaml
  2. For each node, try to match it to a component via identity tags
  3. Assign the node to the correct cluster (inputs/compute/outputs/storage)
  4. Unmatched nodes go to orphans
"""

import fnmatch
import logging
import yaml
from pathlib import Path

from pndocgen.engine.core.models import (
    InventoryNode, Component, ComponentGraph, Edge,
    get_resource_meta, ResourceKind, SemanticRole
)
from pndocgen.engine.core.config import load_resource_kind_overrides, get_app_config

logger = logging.getLogger(__name__)


# Mapping from SemanticRole to D2 cluster names
_ROLE_TO_CLUSTER = {
    SemanticRole.INGRESS:   "inputs",
    SemanticRole.COMPUTE:   "compute",
    SemanticRole.MESSAGING: "messaging",  # usually inputs or outputs, handled dynamically? for now simplified
    SemanticRole.STORAGE:   "storage",
    SemanticRole.SECURITY:  "security",
    SemanticRole.UNKNOWN:   "misc",
    SemanticRole.RELATION:  None,   # EDGEs are not nodes
}

# Messaging roles might need splitting into inputs/outputs based on direction (not known at single-node level).
# For now, we put all messaging middleware in a "messaging" cluster (or distributed if we had topology).
# To align with L3 template (inputs/outputs), we might need heuristics or just map MESSAGING -> messaging
# and let D2 layout handle it. Or map to "outputs" by default?
# Let's stick to "messaging" cluster for SQS/SNS and update template to support it,
# OR map SQS to inputs if it triggers lambda, outputs if produced.
# Since we lack topology here, "messaging" is a safe neutral cluster.


# Match DLQ queues regardless of separator (dash or underscore) and case.
# fnmatch comparison is done case-insensitively (both sides lowercased).
DEFAULT_EXCLUDE_PATTERNS = [
    "*-dlq",       # pn-delivery-insert-trigger-dlq
    "*-dlq.*",     # pn-mandate_inputs-DLQ.fifo
    "*_dlq",       # pn-delivery_insert_trigger_DLQ  (CFN-generated underscore variant)
    "*_dlq.*",     # pn-mandate_expired_trigger_DLQ.fifo
    "*-dead-letter*",
    "*_dead_letter*",
]


class TagResolver:
    """
    Resolves raw InventoryNodes into a ComponentGraph using:
      1. pndocgen.yaml rules (identity tags, kind overrides)
      2. CFN metadata (component assignment)
      3. SemanticRole (cluster assignment)
    """

    def __init__(
        self,
        config_path: Path | None = None,
        pattern: str = "auto",
        include_dlq: bool = False,
    ):
        self.config = self._load_config(config_path)
        self.meta_overrides = load_resource_kind_overrides(config_path)
        self.include_dlq = include_dlq
        self.exclude_patterns = self._build_exclude_patterns()
        self.identity_tags = self.config.get("tag_resolver", {}).get(
            "identity_tags", ["Component", "Service", "Name"]
        )
        _cfg = get_app_config()
        self.name_prefix = self.config.get("tag_resolver", {}).get(
            "name_prefix", _cfg.project.prefix or ""
        )
        self._prefix_stem = self.name_prefix.rstrip("-")  # e.g. "pn" from "pn-"
        # Pattern controls cluster routing (e.g. ecs_microservice → lambda goes to workers)
        self.pattern = pattern

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_config(self, config_path: Path | None) -> dict:
        if config_path and config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            logger.info("Loaded config from %s", config_path)
            return cfg
        logger.info("No pndocgen.yaml found, using defaults")
        return {}

    def _build_exclude_patterns(self) -> list[str]:
        exclude_cfg = self.config.get("exclude", {})
        patterns = list(exclude_cfg.get("name_patterns", DEFAULT_EXCLUDE_PATTERNS))
        if self.include_dlq:
            patterns = [
                pattern
                for pattern in patterns
                if not self._is_dlq_pattern(pattern)
            ]
        return patterns

    @staticmethod
    def _is_dlq_pattern(pattern: str) -> bool:
        p = (pattern or "").lower()
        return "dlq" in p or "dead-letter" in p or "dead_letter" in p

    # ------------------------------------------------------------------
    # Resolution logic
    # ------------------------------------------------------------------

    def resolve(self, nodes: list[InventoryNode]) -> ComponentGraph:
        """Main entry point: resolve a list of raw nodes into a ComponentGraph."""
        graph = ComponentGraph()

        for node in nodes:
            # 1. Exclusion by name pattern (e.g. DLQs)
            if self._is_excluded(node):
                logger.debug("Excluded (pattern): %s", node.name)
                continue

            # 2. Check ResourceKind (SKIP, EDGE, NODE)
            meta = get_resource_meta(node.resource_type, self.meta_overrides)
            
            if meta.kind == ResourceKind.SKIP:
                logger.debug("SKIP (kind): %s (%s)", node.name, node.resource_type)
                continue

            if meta.kind == ResourceKind.EDGE:
                # Create an edge from node metadata if available (e.g. EventSourceMapping)
                # We expect metadata to contain "source" and "target" for relationships
                src = node.metadata.get("source") or node.name
                tgt = node.metadata.get("target") or node.name
                # Edge logic is usually handled by discovery enriching the graph directly,
                # but if an InventoryNode represents an edge (like ESM), we convert it here.
                # Only if source/target are meaningful.
                if src != node.name and tgt != node.name:
                    graph.add_edge(Edge(source=src, target=tgt, edge_type="event_trigger"))
                    logger.debug("Resolved EDGE: %s -> %s", src, tgt)
                else:
                    logger.debug("Skipping malformed EDGE node: %s", node.name)
                continue

            # 3. Resolve Component (Microservice) ownership
            component_name = self._resolve_component_name(node)
            if not component_name:
                graph.orphans.append(node)
                logger.debug("Orphan: %s (no component match)", node.name)
                continue

            # 4. Resolve Cluster (Semantic Role)
            cluster_name = self._resolve_cluster(node)

            if component_name not in graph.components:
                graph.add_component(Component(name=component_name, account=node.account))

            graph.components[component_name].add_node(cluster_name, node)
            logger.debug("Resolved NODE: %s -> %s/%s", node.name, component_name, cluster_name)

        logger.info(
            "Resolution complete: %d components, %d orphans",
            len(graph.components),
            len(graph.orphans),
        )
        return graph

    def reroute_sqs_triggers(self, graph: ComponentGraph, edges: list[Edge]) -> None:
        """
        Move SQS source nodes for trigger/consumer edges from "messaging" to "inputs".

        Applies only to patterns that use the "messaging" cluster.
        Call after edges are added to the graph.

        Args:
            graph: The ComponentGraph to mutate in place.
            edges: The edge list (from inventory relationships).
        """
        # SQS already stays in "queues" for these patterns.
        if self.pattern in ("ecs_microservice", "auto"):
            return

        # Collect all SQS names that feed into a compute node (trigger or consumer)
        trigger_sources: set[str] = {
            e.source for e in edges
            if e.edge_type in ("sqs_trigger", "sqs_consumer")
        }
        if not trigger_sources:
            return

        for comp in graph.components.values():
            messaging = comp.clusters.get("messaging")
            if not messaging:
                continue
            # Find nodes to move to "inputs"
            to_move = [n for n in messaging.nodes if n.name in trigger_sources]
            for node in to_move:
                messaging.nodes.remove(node)
                comp.add_node("inputs", node)
                logger.debug("Rerouted SQS trigger source: %s -> inputs", node.name)

        if trigger_sources:
            logger.info(
                "Rerouted %d SQS trigger source(s) to 'inputs' cluster",
                len(trigger_sources),
            )

    def _is_excluded(self, node: InventoryNode) -> bool:
        """Check if a node should be excluded based on name patterns."""
        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(node.name.lower(), pattern.lower()):
                return True
        return False

    def _resolve_component_name(self, node: InventoryNode) -> str | None:
        """
        Try to find the component name for a node.
        Priority order:
          1. CFN metadata (metadata["component"]) — strictly trustworthy from CfnLiveDiscoverer
          2. Exact tag match (Microservice, Component, Service)
          3. Name prefix match (heuristic)
        """
        # Priority 1: CFN metadata (most reliable source)
        if node.metadata.get("component"):
            return node.metadata["component"]

        # Priority 2: exact tag match
        for tag_key in self.identity_tags:
            if tag_key.lower() == "name":
                continue  # Name is handled as fallback
            value = node.get_tag(tag_key)
            if value and value.startswith(self.name_prefix):
                return value

        # Priority 3: infer from Name tag / resource name
        name = node.name
        # Heuristic: try to extract "pn-{service}" from resource name
        parts = name.split("-")
        if self._prefix_stem and len(parts) >= 3 and parts[0] == self._prefix_stem:
            # "pn-delivery-push-something" -> "pn-delivery-push"
            return "-".join(parts[:3])
        elif self._prefix_stem and len(parts) >= 2 and parts[0] == self._prefix_stem:
            return "-".join(parts[:2])

        return None

    # v60 golden: each resource TYPE gets its own dedicated cluster box.
    # This produces the 6-box layout approved in golden_v60_pn_delivery_L3.d2.
    _ECS_V60_CLUSTER_MAP: dict[str, str] = {
        "apigw":            "apigw",
        "apigw_v2":         "apigw",
        "sqs":              "queues",
        "eventbridge_rule": "rules",
        "eventbridge_bus":  "rules",
        "ecs_service":      "ecs",
        "lambda":           "lambdas",
        "dynamodb":         "dynamodb",
        "s3":               "storage",
        "sns":              "queues",      # SNS topics are input channels
        "kinesis":          "queues",      # Kinesis streams are input channels
        "step_function":    "lambdas",     # Step Functions are compute workers
    }

    def _resolve_cluster(self, node: InventoryNode) -> str:
        """
        Assign a node to a cluster based on its SemanticRole and the active pattern.

        Pattern "ecs_microservice" (v60 golden layout):
          Uses ``_ECS_V60_CLUSTER_MAP`` to route each resource TYPE to a dedicated
          cluster box, producing the 6-box layout:
            apigw   → "API Gateway"
            queues  → "SQS Queues"
            rules   → "EventBridge Rules"
            ecs     → "ECS Microservice"
            lambdas → "Lambda Functions"
            dynamodb → "DynamoDB Tables"

        All other patterns use the flat _ROLE_TO_CLUSTER mapping.
        """
        meta = get_resource_meta(node.resource_type, self.meta_overrides)

        # Apply v74 cluster routing for ecs_microservice AND auto (auto defaults to this layout
        # when an ECS service is present, which is the common case for SEND microservices).
        if self.pattern in ("ecs_microservice", "auto"):
            v60_cluster = self._ECS_V60_CLUSTER_MAP.get(node.resource_type)
            if v60_cluster:
                return v60_cluster

        # Standard cluster assignment (non-ecs patterns or unmapped types)
        if meta.role == SemanticRole.MESSAGING:
            return "messaging"

        return _ROLE_TO_CLUSTER.get(meta.role, "misc")
