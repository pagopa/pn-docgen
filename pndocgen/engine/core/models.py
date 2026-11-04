"""
pn-docgen - Core Data Models

Contains:
  - ResourceKind / SemanticRole / RenderHint: taxonomy enums
  - ResourceMeta: per-type metadata (kind, role, render hint, icon)
  - DEFAULT_RESOURCE_REGISTRY: built-in classification for all known AWS types
  - get_resource_kind(): lookup with project-level overrides
  - InventoryNode: raw discovered AWS resource
  - Edge: directed relationship between two nodes
  - ComponentAnalysis: result of CFN static analysis
  - Component / ComponentGraph: resolved diagram model
"""

from dataclasses import dataclass, field
from enum import Enum
import json
from typing import Optional


# ---------------------------------------------------------------------------
# Taxonomy: how each resource_type appears in a diagram
# ---------------------------------------------------------------------------

class ResourceKind(Enum):
    """
    Top-level classification of a resource in the diagram.

    NODE  → rendered as a box/icon (compute, storage, messaging, ingress)
    EDGE  → rendered as a directed arrow between two existing nodes
    SKIP  → excluded from the diagram entirely
    """
    NODE = "node"
    EDGE = "edge"
    SKIP = "skip"


class SemanticRole(Enum):
    """
    Semantic category of a NODE resource.
    Drives automatic cluster assignment in the diagram layout.

    Follows the standard C4/AWS architecture layer model:
      INGRESS   → external entry points (API GW, ALB, WAF, CloudFront)
      COMPUTE   → processing units (Lambda, ECS, Step Functions)
      MESSAGING → async communication (SQS, SNS, Kinesis, EventBridge)
      STORAGE   → persistent state (DynamoDB, S3, RDS, ElastiCache)
      SECURITY  → auth/authz/encryption (Cognito, WAF — when shown)
      RELATION  → for EDGE-kind resources (EventSourceMapping, etc.)
      UNKNOWN   → fallback for unrecognized types
    """
    INGRESS   = "ingress"
    COMPUTE   = "compute"
    MESSAGING = "messaging"
    STORAGE   = "storage"
    SECURITY  = "security"
    RELATION  = "relation"
    UNKNOWN   = "unknown"


class RenderHint(Enum):
    """
    Hint to the diagram generator on how to render this resource.
    Decoupled from the generator backend (D2, Mermaid, PlantUML, etc.).

    BOX           → standard labeled rectangle
    ICON          → AWS service icon (when icon set is available)
    ARROW         → solid directed arrow (for EDGE resources)
    DASHED_ARROW  → dashed arrow (optional/async relationships)
    CLUSTER       → grouping container (e.g. ECS cluster)
    NONE          → not rendered
    """
    BOX          = "box"
    ICON         = "icon"
    ARROW        = "arrow"
    DASHED_ARROW = "dashed_arrow"
    CLUSTER      = "cluster"
    NONE         = "none"


@dataclass(frozen=True)
class ResourceMeta:
    """
    Full metadata for a single resource_type.

    Attributes:
        kind:        NODE | EDGE | SKIP
        role:        SemanticRole — drives cluster assignment
        render_hint: RenderHint — drives visual rendering
        icon:        optional icon identifier (e.g. "aws-lambda", "aws-sqs")
                     used by icon-aware generators
    """
    kind:        ResourceKind
    role:        SemanticRole  = SemanticRole.UNKNOWN
    render_hint: RenderHint    = RenderHint.ICON
    icon:        str           = ""


def _node(role: SemanticRole, icon: str = "") -> ResourceMeta:
    """Shorthand: create a NODE ResourceMeta."""
    return ResourceMeta(kind=ResourceKind.NODE, role=role, render_hint=RenderHint.ICON, icon=icon)


def _edge(icon: str = "") -> ResourceMeta:
    """Shorthand: create an EDGE ResourceMeta."""
    return ResourceMeta(kind=ResourceKind.EDGE, role=SemanticRole.RELATION,
                        render_hint=RenderHint.DASHED_ARROW, icon=icon)


def _skip() -> ResourceMeta:
    """Shorthand: create a SKIP ResourceMeta."""
    return ResourceMeta(kind=ResourceKind.SKIP, role=SemanticRole.UNKNOWN,
                        render_hint=RenderHint.NONE)


# Built-in registry: resource_type → ResourceMeta
# Override per-project via pndocgen.yaml (resource_kinds section).
#
# Design principle: unknown types default to NODE (never silently dropped).
# This is intentionally more permissive than mingrammer/diagrams, which
# requires explicit registration of every type.
DEFAULT_RESOURCE_REGISTRY: dict[str, ResourceMeta] = {
    # --- Compute
    "lambda":                           _node(SemanticRole.COMPUTE,   "aws-lambda"),
    "ecs_service":                      _node(SemanticRole.COMPUTE,   "aws-ecs"),
    "step_function":                    _node(SemanticRole.COMPUTE,   "aws-step-functions"),
    "glue_job":                         _node(SemanticRole.COMPUTE,   "aws-glue"),
    "batch_job":                        _node(SemanticRole.COMPUTE,   "aws-batch"),
    # --- Storage
    "dynamodb":                         _node(SemanticRole.STORAGE,   "aws-dynamodb"),
    "s3":                               _node(SemanticRole.STORAGE,   "aws-s3"),
    "aws_s3tables_tablebucket":          _node(SemanticRole.STORAGE,   "aws-s3-tables"),
    "aws_s3tables_table":                _node(SemanticRole.STORAGE,   "aws-s3-tables"),
    "aws_s3tables_namespace":            _skip(),
    "aws_kinesisfirehose_deliverystream": _node(SemanticRole.MESSAGING, "aws-firehose"),
    "eventbridge_pipe":                 _node(SemanticRole.MESSAGING, "aws-pipes"),
    "aws_events_eventbus":              _node(SemanticRole.MESSAGING, "aws-eventbridge"),
    "elasticache":                      _node(SemanticRole.STORAGE,   "aws-elasticache"),
    "rds":                              _node(SemanticRole.STORAGE,   "aws-rds"),
    "opensearch":                       _node(SemanticRole.STORAGE,   "aws-opensearch"),
    "efs":                              _node(SemanticRole.STORAGE,   "aws-efs"),
    # --- Messaging / Streaming
    "sqs":                              _node(SemanticRole.MESSAGING, "aws-sqs"),
    "sns":                              _node(SemanticRole.MESSAGING, "aws-sns"),
    "kinesis":                          _node(SemanticRole.MESSAGING, "aws-kinesis"),
    "eventbridge_rule":                 _node(SemanticRole.MESSAGING, "aws-eventbridge"),
    "eventbridge_bus":                  _node(SemanticRole.MESSAGING, "aws-eventbridge"),
    # A schedule is a trigger node even when no target binding is available.
    "aws_scheduler_schedule":           _node(SemanticRole.MESSAGING, "aws-eventbridge"),
    # API authorization is meaningful; keep it until a verified binding can
    # replace the configuration node with an explicit authorization relation.
    "aws_apigateway_authorizer":        _node(SemanticRole.SECURITY, "aws-api-gateway"),
    # --- Ingress / Network
    "apigw":                            _node(SemanticRole.INGRESS,   "aws-api-gateway"),
    "apigw_v2":                         _node(SemanticRole.INGRESS,   "aws-api-gateway"),
    "alb":                              _node(SemanticRole.INGRESS,   "aws-alb"),
    "nlb":                              _node(SemanticRole.INGRESS,   "aws-nlb"),
    "target_group":                     _node(SemanticRole.INGRESS,   "aws-alb"),
    "cloudfront":                       _node(SemanticRole.INGRESS,   "aws-cloudfront"),
    # --- Security
    "aws_wafv2_webacl":                 _node(SemanticRole.SECURITY,  "aws-waf"),
    "cognito_user_pool":                _node(SemanticRole.SECURITY,  "aws-cognito"),
    # --- Relationships → rendered as directed edges, not boxes
    "aws_lambda_eventsourcemapping":    _edge("aws-lambda"),
    # --- Implementation details → excluded from diagrams
    "aws_lambda_layerversion":          _skip(),
    "aws_cloudformation_stack":         _skip(),
    "aws_apigateway_model":             _skip(),
    "aws_apigateway_requestvalidator":  _skip(),
    "aws_apigateway_method":            _skip(),
    "aws_apigateway_resource":          _skip(),
    "aws_apigateway_deployment":        _skip(),
    "aws_apigateway_stage":             _skip(),
    "aws_s3_bucketpolicy":              _skip(),
    "aws_sqs_queuepolicy":              _skip(),
}


from pndocgen.engine.core.resource_policy import CFN_SUPPORT_TYPES

# The same support-type policy applies to saved inventories and live discovery.
for _cfn_type in CFN_SUPPORT_TYPES:
    DEFAULT_RESOURCE_REGISTRY.setdefault(_cfn_type.lower().replace("::", "_"), _skip())
DEFAULT_RESOURCE_REGISTRY.setdefault("cloudwatch_alarm", _skip())
DEFAULT_RESOURCE_REGISTRY.setdefault("log_group", _skip())

# Fallback for any resource_type not in the registry.
# Defaults to NODE/UNKNOWN so nothing is silently dropped.
_FALLBACK_META = ResourceMeta(
    kind=ResourceKind.NODE,
    role=SemanticRole.UNKNOWN,
    render_hint=RenderHint.BOX,
    icon="",
)


def get_resource_meta(
    resource_type: str,
    overrides: dict[str, ResourceMeta] | None = None,
) -> ResourceMeta:
    """
    Return the ResourceMeta for a given resource_type.

    Lookup order (highest priority first):
    1. overrides (from pndocgen.yaml, loaded by config.py)
    2. DEFAULT_RESOURCE_REGISTRY
    3. _FALLBACK_META (NODE/UNKNOWN — never silently dropped)
    """
    if overrides and resource_type in overrides:
        return overrides[resource_type]
    return DEFAULT_RESOURCE_REGISTRY.get(resource_type, _FALLBACK_META)


def get_resource_kind(
    resource_type: str,
    overrides: dict[str, ResourceMeta] | None = None,
) -> ResourceKind:
    """Convenience wrapper: return only the ResourceKind."""
    return get_resource_meta(resource_type, overrides).kind


# ---------------------------------------------------------------------------
# Raw layer: one node per AWS resource
# ---------------------------------------------------------------------------

@dataclass
class InventoryNode:
    """Represents a single raw AWS resource as discovered from AWS APIs."""

    id: str                    # ARN or unique identifier
    name: str                  # Human-readable name (from Name tag or resource name)
    resource_type: str         # Normalized type: ecs_service | lambda | sqs | sns | dynamodb | s3 | ...
    account: str               # AWS account alias (e.g. "core", "confinfo")
    region: str
    tags: dict = field(default_factory=dict)      # Raw AWS tags {Key: Value}
    metadata: dict = field(default_factory=dict)  # Extra fields (ARN, cluster, etc.)

    def get_tag(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Case-insensitive tag lookup."""
        for k, v in self.tags.items():
            if k.lower() == key.lower():
                return v
        return default


# ---------------------------------------------------------------------------
# Edge: directed relationship between two resources
# ---------------------------------------------------------------------------

@dataclass
class Edge:
    """
    A directed relationship between two resources or components.
    Used to represent SQS producer/consumer and HTTP client calls.
    """
    source: str          # resource name or component name
    target: str          # resource name or component name
    edge_type: str       # "sqs_produce" | "sqs_consume" | "http_call"
    label: str = ""      # optional label for the diagram arrow
    evidence: str = ""   # source of inference; empty for historical inventories
    source_ref: dict | None = None
    target_ref: dict | None = None
    evidence_details: dict = field(default_factory=dict)

    def __post_init__(self):
        self.source_ref = endpoint_reference(self.source_ref)
        self.target_ref = endpoint_reference(self.target_ref)
        if not isinstance(self.evidence_details, dict):
            raise ValueError('evidence_details must be a mapping')
        self.evidence_details = json.loads(json.dumps(self.evidence_details, sort_keys=True, allow_nan=False))

    def to_dict(self) -> dict:
        result = {
            "source": self.source,
            "target": self.target,
            "type": self.edge_type,
            "label": self.label,
        }
        if self.evidence:
            result["evidence"] = self.evidence
        if self.evidence_details:
            result["evidence_details"] = self.evidence_details
        for endpoint in ('source', 'target'):
            ref = getattr(self, endpoint + '_ref')
            if ref is not None:
                result[endpoint + '_ref'] = dict(ref)
        return result


def endpoint_reference(value):
    """Version-one additive reference contract; old snapshots omit it."""
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'type', 'id'}
            or not all(isinstance(v, str) and v for v in value.values())
            or not value['type'].startswith('AWS::')):
        raise ValueError(f'Invalid endpoint reference: {value!r}')
    return {'type': value['type'], 'id': value['id']}


# ---------------------------------------------------------------------------
# CFN Analysis layer: static analysis of a microservice repo
# ---------------------------------------------------------------------------

@dataclass
class ComponentAnalysis:
    """
    Result of static CloudFormation analysis of a microservice repository.
    Produced by cfn_analyzer.py, consumed by tag_resolver.py to enrich the graph.
    """
    name: str                          # e.g. "pn-delivery"
    owned_queues: list = field(default_factory=list)     # SQS queues defined in storage.yml
    producer_queues: list = field(default_factory=list)  # queues where this service has sqs:SendMessage
    consumer_queues: list = field(default_factory=list)  # queues where this service has sqs:ReceiveMessage
    http_clients: list = field(default_factory=list)     # microservices called via HTTP (from *BASEURL env vars)
    lambdas: list = field(default_factory=list)          # Lambda function names defined in CFN
    s3_buckets: list = field(default_factory=list)       # S3 buckets owned
    internal_deps: list = field(default_factory=list)    # Retained for snapshot compatibility.
    metadata: dict = field(default_factory=dict)         # plugin-specific signals (e.g. uses_sqs)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "owned_queues": self.owned_queues,
            "producer_queues": self.producer_queues,
            "consumer_queues": self.consumer_queues,
            "http_clients": self.http_clients,
            "lambdas": self.lambdas,
            "s3_buckets": self.s3_buckets,
            "internal_deps": self.internal_deps,
        }

    def all_edges(self) -> list:
        """Generate Edge objects from this analysis."""
        edges = []
        for q in self.producer_queues:
            edges.append(Edge(source=self.name, target=q, edge_type="sqs_produce", label="produces"))
        for q in self.consumer_queues:
            edges.append(Edge(source=q, target=self.name, edge_type="sqs_consume", label="consumes"))
        for svc in self.http_clients:
            edges.append(Edge(source=self.name, target=svc, edge_type="http_call", label="HTTP"))
        return edges


# ---------------------------------------------------------------------------
# Resolved layer: one component per logical microservice
# ---------------------------------------------------------------------------

@dataclass
class ComponentCluster:
    """A named cluster of resources within a component."""
    name: str                              # inputs | compute | outputs | storage | errors
    nodes: list = field(default_factory=list)  # list[InventoryNode]


# Sequenza semantica delle colonne del diagramma: ingresso → code → calcolo → storage.
CLUSTER_ORDER: tuple[str, ...] = (
    "apigw", "rules", "sns", "streams", "inputs", "messaging", "queues",
    "ecs", "compute", "lambdas", "workers",
    "dynamodb", "storage", "output_queues", "external", "security", "misc",
)

_CLUSTER_RANK: dict[str, int] = {name: rank for rank, name in enumerate(CLUSTER_ORDER)}


def _text_key(value) -> bytes:
    """Confronto byte-wise, indipendente dal locale."""
    return str(value or "").encode("utf-8")


def _cluster_key(name: str) -> tuple[int, bytes]:
    return (_CLUSTER_RANK.get(name, len(CLUSTER_ORDER)), _text_key(name))


def _node_key(node) -> tuple[bytes, bytes, bytes]:
    return (_text_key(node.name), _text_key(node.resource_type), _text_key(node.id))


@dataclass
class Component:
    """
    A logical microservice component, resolved from raw inventory nodes.
    Maps to one L3 diagram.
    """
    name: str                              # e.g. "pn-delivery-push"
    account: str
    clusters: dict = field(default_factory=dict)   # {cluster_name: ComponentCluster}
    analysis: Optional[object] = None              # ComponentAnalysis if CFN was analyzed

    def add_node(self, cluster_name: str, node: InventoryNode):
        if cluster_name not in self.clusters:
            self.clusters[cluster_name] = ComponentCluster(name=cluster_name)
        self.clusters[cluster_name].nodes.append(node)

    def get_cluster(self, name: str) -> ComponentCluster:
        return self.clusters.get(name, ComponentCluster(name=name))

    def canonicalize(self) -> None:
        """Ordina i cluster nella sequenza dichiarata e i nodi per nome."""
        for cluster in self.clusters.values():
            cluster.nodes.sort(key=_node_key)
        self.clusters = {
            name: self.clusters[name]
            for name in sorted(self.clusters, key=_cluster_key)
        }

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict (for component_graph.json)."""
        result = {
            "name": self.name,
            "account": self.account,
            "clusters": {
                cluster_name: [
                    {"id": n.id, "name": n.name, "type": n.resource_type,
                     **({'metadata': label_metadata(n.metadata)} if label_metadata(n.metadata) else {})}
                    for n in cluster.nodes
                ]
                for cluster_name, cluster in self.clusters.items()
            }
        }
        if self.analysis:
            result["analysis"] = self.analysis.to_dict()
        return result


def label_metadata(metadata: dict) -> dict:
    """Preserve versioned display provenance and explicit identity metadata.

    Do not serialize arbitrary inventory metadata or alter old graph files.
    The renderer validates the logical path before using it for presentation.
    """
    if not isinstance(metadata, dict):
        return {}
    result = {}
    if metadata.get('cfn_provenance') == 'stack_membership_v1':
        result.update({key: metadata[key] for key in ('cfn_provenance', 'cfn_logical_path', 'logical_id', 'cfn_type')
                       if key in metadata})
    if metadata.get('resource_identity') is not None:
        result['resource_identity'] = endpoint_reference(metadata['resource_identity'])
        for key in ('external_reference', 'discovered_from'):
            if key in metadata:
                result[key] = metadata[key]
    for key, fields in (
        ('eventbridge_rule', ('EventBusName', 'State', 'EventPattern', 'ScheduleExpression')),
        ('eventbridge_pipe', ('CurrentState', 'DesiredState', 'Source', 'Target')),
    ):
        context = metadata.get(key)
        if context is not None:
            if not isinstance(context, dict):
                raise ValueError(f'Invalid {key} metadata')
            result[key] = {field: context[field] for field in fields if field in context}
    return result


@dataclass
class ComponentGraph:
    """
    The full resolved graph of all components.
    Interchange format between tag_resolver and d2_generator.
    """
    components: dict = field(default_factory=dict)   # {name: Component}
    edges: list = field(default_factory=list)         # list[Edge] - cross-component relationships
    orphans: list = field(default_factory=list)       # list[InventoryNode] - unresolved resources

    def add_component(self, component: Component):
        self.components[component.name] = component

    def add_edge(self, edge: Edge):
        self.edges.append(edge)

    def canonicalize(self) -> None:
        """Impone un ordine totale su componenti, cluster, nodi e archi.

        Senza questo passaggio due discovery identiche producono diagrammi diversi,
        perché l'ordine eredita quello delle risposte delle API AWS.
        """
        for component in self.components.values():
            component.canonicalize()
        self.components = {
            name: self.components[name]
            for name in sorted(self.components, key=_text_key)
        }

        cluster_of: dict[str, str] = {
            node.name: cluster_name
            for component in self.components.values()
            for cluster_name, cluster in component.clusters.items()
            for node in cluster.nodes
        }

        def identity(edge: Edge) -> tuple:
            return (
                _text_key(edge.source),
                _text_key(edge.target),
                _text_key(edge.edge_type),
                _text_key(edge.label),
                _text_key(edge.evidence),
                json.dumps(edge.source_ref, sort_keys=True),
                json.dumps(edge.target_ref, sort_keys=True),
                json.dumps(edge.evidence_details, sort_keys=True),
            )

        def edge_key(edge: Edge) -> tuple:
            return (
                _cluster_key(cluster_of.get(edge.source, "")),
                _cluster_key(cluster_of.get(edge.target, "")),
                *identity(edge),
            )

        unique: dict[tuple, Edge] = {}
        for edge in self.edges:
            unique.setdefault(identity(edge), edge)
        self.edges = sorted(unique.values(), key=edge_key)
        self.orphans.sort(key=_node_key)

    def to_dict(self) -> dict:
        return {
            "components": {
                name: comp.to_dict()
                for name, comp in self.components.items()
            },
            "edges": [e.to_dict() for e in self.edges],
            "orphans": [
                {"id": n.id, "name": n.name, "type": n.resource_type}
                for n in self.orphans
            ]
        }
