"""
pn-docgen - CFN Live Discoverer Plugin (read-only)

Discovers microservice resources by querying CloudFormation stacks on AWS.
Uses only read-only APIs:
  - resourcegroupstaggingapi:GetResources  (fast tag-based stack lookup)
  - cloudformation:ListStackResources      (resources per stack)
  - lambda:ListEventSourceMappings  (for SQS consumer relationships)
  - ecs:DescribeServices            (for HTTP client env vars)
  - sqs:GetQueueAttributes          (for queue metadata)

This plugin produces:
  1. A map of {physical_resource_id -> component_name} for CfnDiscoverySource
  2. ComponentAnalysis enrichment (producers, consumers, HTTP clients) from live data

Why this is better than static CFN analysis:
  - PhysicalResourceId = real deployed names (no CFN variable placeholders)
  - Lambda event source mappings = direct SQS consumer relationships
  - ECS env vars = direct HTTP client relationships
  - Works without access to source repositories
  - Supports nested stacks automatically
"""

import logging
import fnmatch
from typing import Optional

import boto3

from pndocgen.engine.core.config import AppConfig, get_app_config

logger = logging.getLogger(__name__)


def _paged_items(client, operation: str, result_key: str, **params):
    """Use SDK paginator contracts instead of silently truncating AWS results."""
    for page in client.get_paginator(operation).paginate(**params):
        yield from page.get(result_key, [])


def _lambda_name(identifier: str) -> str:
    """Function identity, excluding any version/alias qualifier."""
    if ":function:" in identifier:
        return identifier.split(":function:", 1)[1].split(":", 1)[0]
    return identifier.split(":", 1)[0]


def _statements(document):
    statements = document.get("Statement", [])
    return [statements] if isinstance(statements, dict) else statements

# CFN resource types we care about (captured during stack walk).
# Note: _process_stack captures ALL resources regardless of this set.
# This set is a documentation reference for what we semantically care about.
_RESOURCE_TYPES = {
    "AWS::SQS::Queue",
    "AWS::Lambda::Function",
    "AWS::ECS::Service",
    "AWS::DynamoDB::Table",
    "AWS::SNS::Topic",
    "AWS::S3::Bucket",
    "AWS::StepFunctions::StateMachine",
    "AWS::Kinesis::Stream",
    "AWS::Events::Rule",
    "AWS::CloudFormation::Stack",          # nested stacks (expanded recursively)
    "AWS::ApiGateway::RestApi",            # REST API (ingress layer)
    "AWS::ElasticLoadBalancingV2::LoadBalancer",   # ALB (ingress chain)
    "AWS::ElasticLoadBalancingV2::TargetGroup",    # TargetGroup (ALB → ECS)
    "AWS::Lambda::Permission",             # captured for API GW → Lambda edge extraction
}


# CFN resource types that are infrastructure noise for architecture diagrams.
# Override via pndocgen.yaml (cfn.skip_types) or --skip-types CLI flag.
# Pass frozenset() to include everything.
from pndocgen.engine.core.resource_policy import CFN_SUPPORT_TYPES

DEFAULT_SKIP_TYPES = CFN_SUPPORT_TYPES


import re as _re

# UUID pattern — used to detect opaque physical IDs
_UUID_RE = _re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    _re.IGNORECASE
)
# Short random suffix pattern (CFN-generated, e.g. "ce6yy2do93", "dbce1c55e18bd5cd")
_OPAQUE_RE = _re.compile(r'^[0-9a-z]{8,32}$', _re.IGNORECASE)

# CFN auto-generated resource name suffix: ends with dash + 8-13 alphanumeric uppercase chars
# e.g. "pn-delivery-microsvc-dev-PnDeliveryLimitConfigurerQ-kFjvR1vLGBZ8"
#                                                                         ^^^^^^^^^^^^
_CFN_SUFFIX_RE = _re.compile(r'-[A-Za-z0-9]{8,14}$')

_STORAGE_MODE_READ = "read"
_STORAGE_MODE_WRITE = "write"


def _storage_label_from_modes(modes: set[str] | None) -> str:
    """Convert inferred storage modes to a diagram label."""
    if not modes:
        return "uses"
    has_read = _STORAGE_MODE_READ in modes
    has_write = _STORAGE_MODE_WRITE in modes
    if has_read and has_write:
        return "reads/writes"
    if has_read:
        return "reads"
    if has_write:
        return "writes"
    return "uses"


def _classify_storage_modes(
    actions_lower: set[str],
    service: str,
    read_actions: frozenset[str],
    write_actions: frozenset[str],
) -> set[str]:
    """Classify IAM actions into read/write modes for one AWS service."""
    out: set[str] = set()
    prefix = f"{service}:"
    wildcard = f"{service}:*"

    if "*" in actions_lower or wildcard in actions_lower:
        out.update({_STORAGE_MODE_READ, _STORAGE_MODE_WRITE})
        return out

    if actions_lower & read_actions:
        out.add(_STORAGE_MODE_READ)
    if actions_lower & write_actions:
        out.add(_STORAGE_MODE_WRITE)

    # Heuristic for operation families, including partial wildcards
    # (e.g. dynamodb:Get*) and explicit verbs (e.g. dynamodb:DescribeTable).
    for action in actions_lower:
        if not action.startswith(prefix):
            continue
        op = action.split(":", 1)[1]
        op_l = op.lower()
        if op_l.startswith(("get", "query", "scan", "batchget", "transactget", "describe", "list", "head")):
            out.add(_STORAGE_MODE_READ)
        elif op_l.startswith(("put", "update", "delete", "batchwrite", "transactwrite", "write", "create")):
            out.add(_STORAGE_MODE_WRITE)
        elif "*" in op_l:
            out.update({_STORAGE_MODE_READ, _STORAGE_MODE_WRITE})

    return out


def _extract_dynamodb_table_name(resource_arn: str) -> str:
    """Extract DynamoDB table name from table or index ARN."""
    if ":table/" in resource_arn:
        return resource_arn.split(":table/", 1)[1].split("/")[0]
    if "/table/" in resource_arn:
        return resource_arn.split("/table/", 1)[1].split("/")[0]
    return ""


def _normalize_name(physical_id: str, cfn_type: str, logical_id: str) -> str:
    """
    Extract a human-readable name from a CFN physical resource ID.

    Rules by resource type:
    - ECS Service ARN  → service name (last path segment after cluster)
    - Target Group ARN → TG name (before the hash)
    - WAF WebACL       → first pipe-segment (the name)
    - EventBridge Rule → second pipe-segment (the rule name)
    - API GW RestApi   → use logical_id if physical_id is opaque
    - EventSourceMapping UUID → use logical_id
    - Lambda LayerVersion ARN → layer name:version
    - SQS URL          → queue name (last path segment)
    - Generic ARN      → last colon/slash segment
    - Fallback         → logical_id if physical_id looks opaque
    """
    pid = physical_id

    if cfn_type == "AWS::Lambda::Function":
        return _lambda_name(pid)

    # --- ECS Service ARN: arn:aws:ecs:region:account:service/cluster/service-name
    if cfn_type == "AWS::ECS::Service" and pid.startswith("arn:aws:ecs:"):
        parts = pid.split("/")
        if len(parts) >= 3:
            return parts[-1]  # service name

    # --- Target Group ARN: arn:aws:elasticloadbalancing:...:targetgroup/name/hash
    if cfn_type == "AWS::ElasticLoadBalancingV2::TargetGroup" and "targetgroup/" in pid:
        parts = pid.split("/")
        if len(parts) >= 3:
            return parts[-2]  # name is second-to-last segment

    # --- WAF WebACL: name|uuid|REGIONAL
    if cfn_type in ("AWS::WAFv2::WebACL", "AWS::WAFv2::WebACLAssociation") and "|" in pid:
        return pid.split("|")[0]

    # --- EventBridge Rule: bus-name|rule-name  or  rule-name
    if cfn_type == "AWS::Events::Rule" and "|" in pid:
        return pid.split("|")[-1]

    # --- SQS Queue URL: https://sqs.region.amazonaws.com/account/queue-name
    if cfn_type == "AWS::SQS::Queue" and pid.startswith("https://sqs."):
        queue_name = pid.rstrip("/").split("/")[-1]
        # Strip CFN-generated suffix if logical_id is more readable.
        # e.g. "pn-delivery-microsvc-dev-PnDeliveryLimitConfigurerQ-kFjvR1vLGBZ8"
        #   → logical_id "PnDeliveryLimitConfigurerQueue" (ignored, too opaque)
        # But if queue_name still contains opaque stack prefix, strip the random suffix:
        if _CFN_SUFFIX_RE.search(queue_name):
            cleaned = _CFN_SUFFIX_RE.sub("", queue_name)
            # Only keep cleaned name if it doesn't look like a bare stack name
            if cleaned and not cleaned.endswith("-dev") and not cleaned.endswith("-prod"):
                return cleaned
        return queue_name

    # --- Lambda EventSourceMapping: UUID → use logical_id
    if cfn_type == "AWS::Lambda::EventSourceMapping":
        return logical_id or pid

    # --- Lambda LayerVersion ARN: arn:aws:lambda:...:layer:name:version
    if cfn_type == "AWS::Lambda::LayerVersion" and pid.startswith("arn:aws:lambda:"):
        parts = pid.split(":")
        if len(parts) >= 8:
            return f"{parts[-2]}:{parts[-1]}"  # layer-name:version

    # --- Generic ARN: take last colon segment, then last slash segment
    if pid.startswith("arn:"):
        candidate = pid.split(":")[-1].split("/")[-1]
        # If still opaque (hash), fall back to logical_id
        if _UUID_RE.match(candidate) or _OPAQUE_RE.match(candidate):
            return logical_id or candidate
        return candidate

    # --- Opaque short IDs (API GW IDs, CFN hashes): use logical_id
    if _UUID_RE.match(pid) or _OPAQUE_RE.match(pid):
        return logical_id or pid

    # --- Pipe-separated: take last segment
    if "|" in pid:
        return pid.split("|")[-1]

    # --- Default: last colon/slash segment
    return pid.split(":")[-1].split("/")[-1] or logical_id or pid


class CfnLiveDiscoverer:
    """
    Read-only CloudFormation-based resource discoverer.

    Builds a map: physical_resource_id -> component_name
    by walking CFN stacks tagged with Microservice=<name>.

    Also enriches ComponentAnalysis with live relationship data:
      - Lambda event source mappings → SQS consumer edges
      - ECS service env vars → HTTP client edges
    """

    def __init__(
        self,
        session: boto3.Session,
        region: str,
        tag_key: str = "Microservice",
        max_nested_depth: int = 3,
        app_config: Optional[AppConfig] = None,
    ):
        self.cfn = session.client("cloudformation", region_name=region)
        self.lmb = session.client("lambda", region_name=region)
        self.ecs = session.client("ecs", region_name=region)
        self.iam = session.client("iam", region_name=region)
        self.sqs = session.client("sqs", region_name=region)
        self.tagging = session.client("resourcegroupstaggingapi", region_name=region)
        self.apigw_client = session.client("apigateway", region_name=region)
        self.tag_key = tag_key
        self.max_nested_depth = max_nested_depth
        self.region = region
        self._app_config = app_config or get_app_config()
        self.issues: list[dict] = []

        # Built by discover(): physical_resource_id -> component_name
        self.resource_map: dict[str, str] = {}
        # Built by discover(): component_name -> list of physical resource ids
        self.component_resources: dict[str, list[dict]] = {}
        # SQS consumer edges: lambda_arn -> queue_url
        self.lambda_sqs_triggers: list[dict] = []
        self.lambda_sqs_consumers: dict[str, list[str]] = {}
        self.lambda_sqs_producers: dict[str, list[str]] = {}
        # ECS HTTP client edges: component -> [target_component]
        self.ecs_http_clients: dict[str, list[str]] = {}
        # ECS SQS consumer edges: component -> [queue_name]
        # Populated by _enrich_ecs_env_vars() by scanning env var VALUES for SQS URLs.
        # ECS polls queues via application code (no AWS EventSourceMapping),
        # so the only discovery path is via task definition environment variables.
        self.ecs_sqs_consumers: dict[str, list[str]] = {}
        # Tracks stacks already walked to avoid double-processing nested stacks
        # (Resource Groups Tagging API returns both root and nested stacks when tag is propagated)
        self._visited_stacks: set[str] = set()
        # Reverse lookup: raw_queue_name (last segment of URL or ARN) → normalized_name
        # Populated by to_inventory_nodes() so to_edges() can resolve IAM ARN queue names.
        self._queue_rawname_to_norm: dict[str, str] = {}
        self._resource_aliases: dict[tuple[str, str], set[str]] = {}
        self._ecs_component_names: dict[str, set[str]] = {}
        # Lambda execution role IAM scan → storage access edges
        # Maps lambda_fn_name → list of storage resource names (DynamoDB table, S3 bucket)
        self.lambda_storage_access: dict[str, list[str]] = {}
        # Storage access mode per Lambda/resource inferred from IAM actions.
        # lambda_fn_name -> resource_name -> {"read", "write"}
        self.lambda_storage_modes: dict[str, dict[str, set[str]]] = {}
        # API Gateway → Lambda integration edges (from Lambda resource-based policy)
        # List of {"apigw_name": ..., "lambda_name": ...}
        self.apigw_lambda_edges: list[dict] = []
        # ECS task role IAM scan → storage access edges (DynamoDB/S3)
        # Maps ecs_component_name → list of storage resource names
        self.ecs_storage_access: dict[str, list[str]] = {}
        # Storage access mode per ECS/resource inferred from IAM actions.
        # ecs_component_name -> resource_name -> {"read", "write"}
        self.ecs_storage_modes: dict[str, dict[str, set[str]]] = {}
        # EventBridge Rules → ECS edges (from events:ListTargetsByRule)
        # List of {"rule_name": ..., "ecs_component": ..., "component": ...}
        self.eventbridge_ecs_edges: list[dict] = []
        # EventBridge Rules → Lambda edges
        # List of {"rule_name": ..., "lambda_name": ..., "component": ...}
        self.eventbridge_lambda_edges: list[dict] = []
        # EventBridge Rules → SQS edges
        # List of {"rule_name": ..., "queue_name": ..., "component": ...}
        self.eventbridge_sqs_edges: list[dict] = []
        # API GW → ECS edges (via ALB/TargetGroup inspection)
        # List of {"apigw_name": ..., "ecs_component": ...}
        self.apigw_ecs_edges: list[dict] = []
        # DynamoDB Stream → Lambda edges (EventSourceMapping, :dynamodb: source)
        # List of {"lambda_arn": ..., "table_arn": ..., "component": ...}
        self.lambda_dynamodb_triggers: list[dict] = []
        self.lambda_kinesis_triggers: list[dict] = []
        # ECS SQS producer edges (sqs:SendMessage IAM actions) — component → [queue_name]
        self.ecs_sqs_producers: dict[str, list[str]] = {}
        # SNS fan-out edges: SNS topic → SQS queue / Lambda function
        # List of {"source_topic": ..., "target": ..., "target_type": "sqs"|"lambda"}
        self.sns_edges: list[dict] = []
        # EventBridge client (needed for list_targets_by_rule)
        self.events = session.client("events", region_name=region)
        # SNS client (needed for list_subscriptions_by_topic)
        self.sns = session.client("sns", region_name=region)
        # Real API Gateway names: api_id → real_name (from apigateway:GetRestApi)
        # physical_id for AWS::ApiGateway::RestApi is just the API ID (e.g. "dil41w79vb")
        # which _normalize_name() can't resolve. This dict provides the real name.
        self.apigw_real_names: dict[str, str] = {}

    def _record_issue(self, stage: str, resource: str, error) -> None:
        issue = {"stage": stage, "resource": resource, "error": str(error)}
        if issue not in self.issues:
            self.issues.append(issue)
        logger.warning("[cfn-live] Partial discovery: %s for %s: %s", stage, resource, error)

    def _edge_resource_name(self, cfn_type: str, identifier: str) -> str:
        names = self._resource_aliases.get((cfn_type, identifier), set())
        if len(names) > 1:
            raise ValueError(f"Ambiguous relationship endpoint: {cfn_type} {identifier}")
        return next(iter(names), identifier)

    def _ecs_endpoint(self, component: str) -> str:
        """Resolve a component-level inference only when its ECS owner is unique."""
        names = self._ecs_component_names.get(component, set())
        if len(names) > 1:
            raise ValueError(f"Ambiguous ECS owner in {component}: {sorted(names)}")
        return next(iter(names), component)

    def discover(self, component_filter: Optional[str] = None) -> None:
        """
        Walk all CFN stacks tagged with Microservice=<name>.
        Populates resource_map and component_resources.

        Args:
            component_filter: if set, only process stacks for this component
        """
        logger.info("[cfn-live] Listing stacks via Resource Groups Tagging API (tag: %s)", self.tag_key)
        stacks = self._list_tagged_stacks(component_filter)
        logger.info("[cfn-live] Found %d stacks", len(stacks))

        for stack in stacks:
            stack_name = stack["stack_name"]
            component = stack["component"]
            logger.info("[cfn-live] Processing stack %s -> %s", stack_name, component)
            self._process_stack(stack_name, component, depth=0)

        # Enrich with live relationship data
        self._enrich_apigw_names()      # must run first — populates apigw_real_names for to_inventory_nodes()
        self._enrich_lambda_triggers()
        self._enrich_ecs_env_vars()
        self._enrich_lambda_iam()
        self._enrich_apigw_lambda()
        self._enrich_eventbridge_targets()
        self._enrich_apigw_ecs()
        self._enrich_sns_subscriptions()

        logger.info(
            "[cfn-live] Discovery complete: %d resources across %d components",
            len(self.resource_map),
            len(self.component_resources),
        )


    def to_inventory_nodes(
        self,
        account: str = "",
        region: str = "",
        skip_cfn_types: frozenset | None = None,
    ) -> list:
        """
        Convert discovered CFN resources into InventoryNode objects.
        Use this instead of AWSDiscoverer when --discovery-source cfn is active.

        Args:
            account: AWS account alias (e.g. "core")
            region: AWS region (e.g. "eu-south-1")
            skip_cfn_types: set of CFN resource types to exclude.
                            Defaults to DEFAULT_SKIP_TYPES.
                            Pass frozenset() to include everything.
        """
        from pndocgen.engine.core.models import InventoryNode

        if skip_cfn_types is None:
            skip_cfn_types = DEFAULT_SKIP_TYPES

        # Map CFN resource types to pndocgen resource_type strings
        _TYPE_MAP = {
            "AWS::SQS::Queue": "sqs",
            "AWS::Lambda::Function": "lambda",
            "AWS::ECS::Service": "ecs_service",
            "AWS::DynamoDB::Table": "dynamodb",
            "AWS::SNS::Topic": "sns",
            "AWS::S3::Bucket": "s3",
            "AWS::StepFunctions::StateMachine": "step_function",
            "AWS::Kinesis::Stream": "kinesis",
            "AWS::Events::Rule": "eventbridge_rule",
            "AWS::Scheduler::Schedule": "aws_scheduler_schedule",
            "AWS::ElasticLoadBalancingV2::LoadBalancer": "alb",
            "AWS::ElasticLoadBalancingV2::TargetGroup": "target_group",
            "AWS::ApiGateway::RestApi": "apigw",
            "AWS::ApiGatewayV2::Api": "apigw_v2",
            "AWS::CloudWatch::Alarm": "cloudwatch_alarm",
            "AWS::Logs::LogGroup": "log_group",
            "AWS::Pipes::Pipe": "eventbridge_pipe",
        }

        seen_ids: set[tuple[str, str]] = set()
        seen_names: dict[tuple[str, str], tuple[str, str]] = {}
        nodes = []
        for component, resources in self.component_resources.items():
            for res in resources:
                physical_id = res["physical_id"]
                cfn_type = res.get("type", "")
                logical_id = res.get("logical_id", "")

                # Skip nested stacks (already expanded) and duplicates
                if cfn_type == "AWS::CloudFormation::Stack":
                    continue
                if cfn_type in skip_cfn_types:
                    continue
                identity = (cfn_type, physical_id)
                if identity in seen_ids:
                    continue

                seen_ids.add(identity)

                resource_type = _TYPE_MAP.get(cfn_type, cfn_type.lower().replace("::", "_"))
                name = _normalize_name(physical_id, cfn_type, logical_id)

                # For API Gateway REST APIs the physical_id is an opaque API ID (e.g. "dil41w79vb").
                # _normalize_name() falls back to logical_id ("PublicRestApiOpenapi") which is
                # identical for every microservice. Override with the real name from AWS.
                if cfn_type in ("AWS::ApiGateway::RestApi", "AWS::ApiGatewayV2::Api"):
                    real_name = self.apigw_real_names.get(physical_id)
                    if real_name:
                        name = real_name

                # ECS services are often named with a CFN-generated opaque suffix
                # (e.g. "pn-delivery-microsvc-dev-DeliveryMicroservice-XYZ-ECSService-ABC").
                # Replace with the component name, which is the human-readable service name
                # (e.g. "pn-delivery").
                if cfn_type == "AWS::ECS::Service" and "-ECSService-" in name:
                    name = component

                # Any resource whose normalized name still contains an env-stage token
                # (e.g. "-dev-", "-prod-") is still opaque / CFN-generated.
                # Convert PascalCase logical_id to kebab-case for human-readable names.
                # This covers SQS queues, EventBridge rules/pipes, and any other resource
                # type where _normalize_name didn't fully clean the physical_id.
                env_tokens = ("-dev-", "-prod-", "-staging-", "-uat-")
                if logical_id and (cfn_type == "AWS::Pipes::Pipe" or any(tok in name for tok in env_tokens)):
                    import re as _re2
                    # Convert PascalCase logical_id → kebab-case
                    # e.g. "PnDeliveryLimitConfigurerQueue" → "pn-delivery-limit-configurer-queue"
                    kebab = _re2.sub(r"(?<!^)(?=[A-Z])", "-", logical_id).lower()
                    # Only keep kebab if it's not just lowercase noise (e.g. "alarm")
                    if kebab and len(kebab) > 3:
                        name = kebab

                # Secondary dedup: same (component, name) from different stacks
                # (e.g. 3 API GW RestApi entries all resolving to "PublicRestApiOpenapi")
                name_key = (component, name)
                if name_key in seen_names:
                    raise ValueError(
                        f"Ambiguous resource name in {component}: {name}; distinct physical identities: "
                        f"{seen_names[name_key]!r} versus {identity!r}")
                seen_names[name_key] = identity
                if cfn_type == "AWS::ECS::Service":
                    self._ecs_component_names.setdefault(component, set()).add(name)
                for alias in {physical_id, physical_id.split(":")[-1].split("/")[-1],
                              _normalize_name(physical_id, cfn_type, logical_id), name}:
                    self._resource_aliases.setdefault((cfn_type, alias), set()).add(name)

                nodes.append(InventoryNode(
                    id=physical_id,
                    name=name,
                    resource_type=resource_type,
                    account=account,
                    region=region or self.region,
                    tags={self.tag_key: component},
                    metadata={
                        "component": component,
                        "logical_id": logical_id,
                        "cfn_type": cfn_type,
                    }
                ))

                # Build reverse map for SQS: raw URL name → normalized name.
                # This is needed in to_edges() to resolve IAM policy ARN queue names
                # (which are always raw CFN-generated names) back to the normalized
                # human-readable names stored in InventoryNode.name.
                if cfn_type == "AWS::SQS::Queue":
                    raw_url_name = physical_id.rstrip("/").split("/")[-1]
                    self._queue_rawname_to_norm[raw_url_name] = name

        logger.info(
            "[cfn-live] Converted to %d InventoryNodes (deduped from %d raw)",
            len(nodes),
            len(self.component_resources.get(list(self.component_resources.keys())[0], []) if self.component_resources else []),
        )
        return nodes

    def to_edges(self) -> list[dict]:
        """
        Convert discovered relationships into serializable edge dicts.

        Edge types emitted:
          sqs_trigger        — SQS queue → Lambda (EventSourceMapping)
          sqs_consumer       — SQS queue → ECS service (IAM policy, ReceiveMessage)
          sqs_producer       — ECS/Lambda → SQS queue (IAM policy, SendMessage)
          http_call          — ECS service → external microservice (BASEURL env var)
          storage_access     — Lambda/ECS → DynamoDB/S3 (IAM execution role)
          apigw_integration  — API Gateway → Lambda or ECS
          eventbridge_trigger— EventBridge Rule → ECS / Lambda / SQS
          dynamodb_stream    — DynamoDB Stream → Lambda (EventSourceMapping)
          sns_subscription   — SNS Topic → SQS queue / Lambda (subscription)
        """
        edges = []

        # 1. SQS → Lambda (EventSourceMapping)
        for t in self.lambda_sqs_triggers:
            queue_name = t["queue_arn"].split(":")[-1].split("/")[-1]
            queue_name = self._queue_rawname_to_norm.get(queue_name, queue_name)
            lambda_name = _lambda_name(t["lambda_arn"])
            edges.append({
                "source": queue_name,
                "target": lambda_name,
                "type": "sqs_trigger",
                "label": "triggers",
            })

        # 1b. DynamoDB Stream → Lambda (EventSourceMapping, :dynamodb: source)
        # Stream ARN format: arn:aws:dynamodb:region:account:table/TableName/stream/TIMESTAMP
        # Use the same ARN parser as IAM table/index references.
        for t in self.lambda_dynamodb_triggers:
            raw_arn = t["table_arn"]
            table_name = _extract_dynamodb_table_name(raw_arn)
            if not table_name:
                logger.warning("Skipping DynamoDB stream edge: table name could not be resolved")
                continue
            lambda_name = _lambda_name(t["lambda_arn"])
            edges.append({
                "source": table_name,
                "target": lambda_name,
                "type": "dynamodb_stream",
                "label": "stream",
            })

        for mapping in self.lambda_kinesis_triggers:
            arn = mapping["stream_arn"]
            stream = arn.split(":stream/", 1)[-1]
            edges.append({"source": stream, "target": _lambda_name(mapping["lambda_arn"]),
                          "type": "kinesis_trigger", "label": "stream"})

        # 2. SQS → ECS (IAM policy ReceiveMessage — ECS polls the queue)
        # IAM ARNs contain raw CFN queue names (e.g. "pn-delivery-microsvc-dev-PnDelivery...").
        # We resolve them to normalized names using _queue_rawname_to_norm built by
        # to_inventory_nodes(). Edges without a match are dropped (cross-component or DLQ).
        for ecs_component, queue_names in self.ecs_sqs_consumers.items():
            for raw_name in set(queue_names):
                # Resolve raw CFN name → normalized human-readable name
                norm_name = self._queue_rawname_to_norm.get(raw_name, raw_name)
                edges.append({
                    "source": norm_name,
                    "target": self._ecs_endpoint(ecs_component),
                    "type": "sqs_consumer",
                    "label": "consumes",
                })
                if norm_name != raw_name:
                    logger.debug("[cfn-live] sqs_consumer resolved: %r -> %r", raw_name, norm_name)

        # 2b. ECS → SQS (IAM policy SendMessage — ECS writes to queue)
        for ecs_component, queue_names in self.ecs_sqs_producers.items():
            for raw_name in set(queue_names):
                norm_name = self._queue_rawname_to_norm.get(raw_name, raw_name)
                edges.append({
                    "source": self._ecs_endpoint(ecs_component),
                    "target": norm_name,
                    "type": "sqs_producer",
                    "label": "produces",
                })
                if norm_name != raw_name:
                    logger.debug("[cfn-live] sqs_producer resolved: %r -> %r", raw_name, norm_name)

        # 3. ECS → external microservice (HTTP, BASEURL env var)
        for source_comp, targets in self.ecs_http_clients.items():
            for target_comp in set(targets):
                edges.append({
                    "source": self._ecs_endpoint(source_comp),
                    "target": target_comp,
                    "type": "http_call",
                    "label": "HTTP",
                })

        # 4. Lambda → storage resources (DynamoDB, S3) from IAM execution role
        for lambda_name, storage_names in self.lambda_storage_access.items():
            for res_name in set(storage_names):
                modes = self.lambda_storage_modes.get(lambda_name, {}).get(res_name, set())
                edges.append({
                    "source": lambda_name,
                    "target": res_name,
                    "type": "storage_access",
                    "label": _storage_label_from_modes(modes),
                    "evidence": "iam_permission",
                })

        # 4b. ECS → storage resources (DynamoDB, S3) from IAM task role
        for ecs_comp, storage_names in self.ecs_storage_access.items():
            for res_name in set(storage_names):
                modes = self.ecs_storage_modes.get(ecs_comp, {}).get(res_name, set())
                edges.append({
                    "source": self._ecs_endpoint(ecs_comp),
                    "target": res_name,
                    "type": "storage_access",
                    "label": _storage_label_from_modes(modes),
                })

        # 5. API Gateway → Lambda (from Lambda resource-based policy)
        for edge in self.apigw_lambda_edges:
            edges.append({
                "source": edge["apigw_name"],
                "target": edge["lambda_name"],
                "type": "apigw_integration",
                "label": "",
                "evidence": "lambda_resource_policy_permission",
            })

        # 6. EventBridge Rule → ECS (from events:ListTargetsByRule)
        for edge in self.eventbridge_ecs_edges:
            edges.append({
                "source": edge["rule_name"],
                "target": self._ecs_endpoint(edge["ecs_component"]),
                "type": "eventbridge_trigger",
                "label": "triggers",
            })

        # 6b. EventBridge Rule → Lambda
        for edge in self.eventbridge_lambda_edges:
            edges.append({
                "source": edge["rule_name"],
                "target": edge["lambda_name"],
                "type": "eventbridge_trigger",
                "label": "triggers",
            })

        # 6c. EventBridge Rule → SQS
        for edge in self.eventbridge_sqs_edges:
            edges.append({
                "source": edge["rule_name"],
                "target": edge["queue_name"],
                "type": "eventbridge_trigger",
                "label": "triggers",
                "evidence": edge.get("evidence", "eventbridge_target_configuration"),
            })

        # 7. API Gateway → ECS (via ALB TargetGroup)
        for edge in self.apigw_ecs_edges:
            edges.append({
                "source": edge["apigw_name"],
                "target": self._ecs_endpoint(edge["ecs_component"]),
                "type": "apigw_integration",
                "label": "",
                "evidence": "same_component_heuristic",
            })

        # 8. SNS Topic → SQS queue / Lambda (subscription)
        for edge in self.sns_edges:
            edges.append({
                "source": edge["source_topic"],
                "target": edge["target"],
                "type": "sns_subscription",
                "label": "fan-out",
            })

        for producer, queues in self.lambda_sqs_producers.items():
            for queue in sorted(set(queues)):
                edges.append({"source": producer, "target": self._queue_rawname_to_norm.get(queue, queue),
                              "type": "sqs_producer", "label": "produces"})
        for consumer, queues in self.lambda_sqs_consumers.items():
            for queue in sorted(set(queues)):
                edges.append({"source": self._queue_rawname_to_norm.get(queue, queue), "target": consumer,
                              "type": "sqs_consumer", "label": "consumes"})
        # Queue aliases are shared by all relation sources, not just ECS IAM.
        for edge in edges:
            edge.setdefault("evidence", {
                "sqs_trigger": "event_source_mapping_configuration",
                "dynamodb_stream": "event_source_mapping_configuration",
                "kinesis_trigger": "event_source_mapping_configuration",
                "sqs_consumer": "iam_permission",
                "sqs_producer": "iam_permission",
                "http_call": "environment_reference",
                "storage_access": "iam_or_environment_reference",
                "eventbridge_trigger": "eventbridge_target_configuration",
                "sns_subscription": "sns_subscription_configuration",
            }.get(edge["type"], "unknown"))
            kind = edge["type"]
            source_types = {"sqs_trigger": "SQS::Queue", "sqs_consumer": "SQS::Queue",
                            "dynamodb_stream": "DynamoDB::Table", "kinesis_trigger": "Kinesis::Stream",
                            "sns_subscription": "SNS::Topic", "eventbridge_trigger": "Events::Rule",
                            "apigw_integration": "ApiGateway::RestApi"}
            if kind in source_types:
                edge["source"] = self._edge_resource_name("AWS::" + source_types[kind], edge["source"])
            # Identity aliases reconcile CFN display names with relationship ARNs.
            for endpoint in ("source", "target"):
                edge[endpoint] = self._edge_resource_name("AWS::Lambda::Function", edge[endpoint])
            if edge["type"] in {"sqs_trigger", "sqs_consumer"}:
                edge["source"] = self._queue_rawname_to_norm.get(edge["source"], edge["source"])
            if edge["type"] in {"sqs_producer", "sns_subscription", "eventbridge_trigger"}:
                edge["target"] = self._queue_rawname_to_norm.get(edge["target"], edge["target"])
        return edges

    def _register_lambda_storage_access(
        self,
        lambda_name: str,
        resource_name: str,
        modes: set[str] | None,
    ) -> None:
        """Register Lambda->storage access and merge inferred read/write modes."""
        storage_list = self.lambda_storage_access.setdefault(lambda_name, [])
        if resource_name not in storage_list:
            storage_list.append(resource_name)
        if modes:
            mode_map = self.lambda_storage_modes.setdefault(lambda_name, {})
            mode_map.setdefault(resource_name, set()).update(modes)

    def _register_ecs_storage_access(
        self,
        component: str,
        resource_name: str,
        modes: set[str] | None,
    ) -> None:
        """Register ECS->storage access and merge inferred read/write modes."""
        storage_list = self.ecs_storage_access.setdefault(component, [])
        if resource_name not in storage_list:
            storage_list.append(resource_name)
        if modes:
            mode_map = self.ecs_storage_modes.setdefault(component, {})
            mode_map.setdefault(resource_name, set()).update(modes)

    # ------------------------------------------------------------------
    # Stack walking
    # ------------------------------------------------------------------

    def _list_tagged_stacks(self, component_filter: Optional[str]) -> list[dict]:
        """
        Use Resource Groups Tagging API to find CFN stacks tagged with our tag_key.
        Single paginated call — much faster than ListStacks + N×DescribeStacks.
        """
        result = []
        paginator = self.tagging.get_paginator("get_resources")
        filters = [{"Key": self.tag_key, "Values": [component_filter] if component_filter else []}]
        # Remove empty Values list (means "any value for this key")
        tag_filters = [{"Key": self.tag_key}] if not component_filter else [
            {"Key": self.tag_key, "Values": [component_filter]}
        ]

        for page in paginator.paginate(
            TagFilters=tag_filters,
            ResourceTypeFilters=["cloudformation:stack"],
        ):
            for resource in page.get("ResourceTagMappingList", []):
                arn = resource["ResourceARN"]
                tags = {t["Key"]: t["Value"] for t in resource.get("Tags", [])}
                component = tags.get(self.tag_key, "")
                if not component:
                    continue
                # Extract stack name from ARN: arn:aws:cloudformation:region:account:stack/name/id
                stack_name = arn.split("/")[1] if "/" in arn else arn
                result.append({"stack_name": stack_name, "component": component, "arn": arn})

        return result


    def _process_stack(self, stack_name: str, component: str, depth: int) -> None:
        """Recursively process a stack and its nested stacks."""
        if depth > self.max_nested_depth:
            self._record_issue("nested_stack_depth", stack_name, "maximum configured depth exceeded")
            return

        # Skip if already visited — happens when Resource Groups Tagging API returns
        # both root and nested stacks (tag is propagated to all nested stacks)
        if stack_name in self._visited_stacks:
            logger.debug("[cfn-live] Skipping already-visited stack %s", stack_name)
            return
        self._visited_stacks.add(stack_name)

        try:
            paginator = self.cfn.get_paginator("list_stack_resources")
            for page in paginator.paginate(StackName=stack_name):
                for res in page.get("StackResourceSummaries", []):
                    res_type = res.get("ResourceType", "")
                    physical_id = res.get("PhysicalResourceId", "")
                    logical_id = res.get("LogicalResourceId", "")

                    if not physical_id:
                        continue

                    # Register in resource map (physical_id -> component)
                    self.resource_map[physical_id] = component
                    self.component_resources.setdefault(component, []).append({
                        "physical_id": physical_id,
                        "logical_id": logical_id,
                        "type": res_type,
                    })

                    # Recurse into nested stacks (ARN or name)
                    if res_type == "AWS::CloudFormation::Stack":
                        nested_name = physical_id.split("/")[1] if "/" in physical_id else physical_id
                        self._process_stack(nested_name, component, depth + 1)

        except Exception as e:
            self._record_issue("list_stack_resources", stack_name, e)

    # ------------------------------------------------------------------
    # Relationship enrichment (read-only)
    # ------------------------------------------------------------------

    def _enrich_apigw_names(self) -> None:
        """
        Resolve real API Gateway names for all AWS::ApiGateway::RestApi resources.

        The CloudFormation physical resource ID for a RestApi is the opaque API ID
        (e.g. "dil41w79vb"), not the human-readable name.  _normalize_name() falls
        back to the CFN logical ID ("PublicRestApiOpenapi") which is the same for
        every microservice.

        This method calls apigateway:GetRestApi for each discovered RestApi and
        populates self.apigw_real_names = {api_id: real_name}, which is then
        used in to_inventory_nodes() to override the fallback name.
        """
        api_entries: list[tuple[str, str]] = [
            (r["physical_id"], component)
            for component, resources in self.component_resources.items()
            for r in resources
            if r.get("type") in ("AWS::ApiGateway::RestApi",) and r.get("physical_id")
        ]

        if not api_entries:
            logger.debug("[cfn-live] No API Gateway REST APIs found, skipping name enrichment")
            return

        logger.info("[cfn-live] Resolving real names for %d API Gateway REST APIs", len(api_entries))
        for api_id, component in api_entries:
            try:
                resp = self.apigw_client.get_rest_api(restApiId=api_id)
                real_name = resp.get("name", "")
                if real_name:
                    self.apigw_real_names[api_id] = real_name
                    logger.debug("[cfn-live] API GW real name: %s -> %r", api_id, real_name)
            except Exception as e:
                self._record_issue("get_rest_api", api_id, e)

        logger.info(
            "[cfn-live] API GW name enrichment complete: %d names resolved",
            len(self.apigw_real_names),
        )

    def _enrich_lambda_triggers(self) -> None:
        """
        For each Lambda in our resource map, check event source mappings.
        Lambda → SQS trigger = consumer relationship.

        Note: CloudFormation physical resource IDs for AWS::Lambda::Function are
        bare function names (e.g. "pn-delivery-insert-trigger-eb-lambda"), NOT ARNs.
        We therefore iterate component_resources filtered by CFN type, not by ARN pattern.
        """
        # Collect (function_name, component) pairs from component_resources
        lambda_entries: list[tuple[str, str]] = [
            (r["physical_id"], component)
            for component, resources in self.component_resources.items()
            for r in resources
            if r.get("type") == "AWS::Lambda::Function" and r.get("physical_id")
        ]

        logger.info("[cfn-live] Enriching triggers for %d Lambda functions", len(lambda_entries))
        for fn_name, component in lambda_entries:
            try:
                for mapping in _paged_items(self.lmb, "list_event_source_mappings", "EventSourceMappings", FunctionName=fn_name):
                    event_source = mapping.get("EventSourceArn", "")
                    if ":sqs:" in event_source:
                        self.lambda_sqs_triggers.append({
                            "lambda_arn": fn_name,   # bare name — to_edges() handles both
                            "queue_arn": event_source,
                            "component": component,
                        })
                        logger.debug("[cfn-live] SQS trigger: %s <- %s", fn_name, event_source)
                    elif ":dynamodb:" in event_source:
                        self.lambda_dynamodb_triggers.append({
                            "lambda_arn": fn_name,
                            "table_arn": event_source,
                            "component": component,
                        })
                        logger.debug("[cfn-live] DynamoDB stream trigger: %s <- %s", fn_name, event_source)
                    elif ":kinesis:" in event_source and ":stream/" in event_source:
                        self.lambda_kinesis_triggers.append({"lambda_arn": fn_name,
                                                             "stream_arn": event_source, "component": component})
                    else:
                        self._record_issue("unsupported_event_source", fn_name,
                                           "Event source mapping type is not supported")
            except Exception as e:
                self._record_issue("list_event_source_mappings", fn_name, e)

    def _enrich_ecs_env_vars(self) -> None:
        """
        For each ECS service in our resource map:
          1. Read container env vars for *BASEURL* patterns → HTTP client edges.
          2. Read the task role IAM policies for SQS actions → SQS consumer/producer edges.

        Scanning IAM policies on the task role is more reliable than env vars because:
          - Policies contain literal queue ARNs (even when the URL is passed via SSM).
          - sqs:ReceiveMessage / sqs:DeleteMessage → consumer (queue triggers ECS logic).
          - sqs:SendMessage                        → producer (ECS writes to queue).
        """
        import json as _json
        import re

        # Read ECS dependency config from AppConfig (de-hardcoded)
        _ecs_cfg = self._app_config.discovery.ecs_dependencies
        _env_regex = _ecs_cfg.env_regex or r"PN_[A-Z]+_([A-Z_]+?)(?:BASE_URL|BASEURL)$"
        _RE_BASEURL = re.compile(_env_regex, re.IGNORECASE)

        _DYNAMO_READ_ACTIONS = frozenset({
            "dynamodb:getitem",
            "dynamodb:batchgetitem",
            "dynamodb:query",
            "dynamodb:scan",
            "dynamodb:transactgetitems",
            "dynamodb:describe*",
            "dynamodb:list*",
        })
        _DYNAMO_WRITE_ACTIONS = frozenset({
            "dynamodb:putitem",
            "dynamodb:updateitem",
            "dynamodb:deleteitem",
            "dynamodb:batchwriteitem",
            "dynamodb:transactwriteitems",
        })
        _S3_READ_ACTIONS = frozenset({
            "s3:getobject",
            "s3:getobjectversion",
            "s3:listbucket",
            "s3:headobject",
        })
        _S3_WRITE_ACTIONS = frozenset({
            "s3:putobject",
            "s3:deleteobject",
            "s3:deleteobjectversion",
            "s3:abortmultipartupload",
        })

        # Component-scoped storage lookups (avoid cross-component table attribution).
        known_resources_by_component: dict[str, dict[str, str]] = {}
        component_tables_by_name: dict[str, set[str]] = {}
        component_buckets_by_name: dict[str, set[str]] = {}
        for component, resources in self.component_resources.items():
            aliases: dict[str, str] = {}
            owned_tables: set[str] = set()
            owned_buckets: set[str] = set()
            for res in resources:
                cfn_type = res.get("type", "")
                pid = res.get("physical_id", "")
                logical_id = res.get("logical_id", "")
                if cfn_type not in ("AWS::DynamoDB::Table", "AWS::S3::Bucket"):
                    continue
                norm_name = _normalize_name(pid, cfn_type, logical_id)
                fragment = pid.split(":")[-1].split("/")[-1]
                aliases[pid] = norm_name
                aliases[fragment] = norm_name
                aliases[norm_name] = norm_name
                if cfn_type == "AWS::DynamoDB::Table":
                    owned_tables.add(norm_name)
                elif cfn_type == "AWS::S3::Bucket":
                    owned_buckets.add(norm_name)

            known_resources_by_component[component] = aliases
            component_tables_by_name[component] = owned_tables
            component_buckets_by_name[component] = owned_buckets

        ecs_resources = [
            r for r in self.resource_map
            if ":service/" in r  # ECS service ARN pattern
        ]
        for svc_arn in ecs_resources:
            try:
                # Extract cluster and service name from ARN
                # arn:aws:ecs:region:account:service/cluster/service-name
                parts = svc_arn.split("/")
                if len(parts) < 3:
                    continue
                cluster = parts[-2]
                svc_name = parts[-1]

                resp = self.ecs.describe_services(
                    cluster=cluster,
                    services=[svc_name],
                )
                component = self.resource_map.get(svc_arn, "unknown")
                component_known = known_resources_by_component.get(component, {})
                owned_tables = component_tables_by_name.get(component, set())
                owned_buckets = component_buckets_by_name.get(component, set())

                for svc in resp.get("services", []):
                    td_arn = svc.get("taskDefinition", "")
                    if not td_arn:
                        continue

                    # Read task definition
                    td_resp = self.ecs.describe_task_definition(taskDefinition=td_arn)
                    td = td_resp.get("taskDefinition", {})
                    containers = td.get("containerDefinitions", [])

                    # 1. BASEURL env vars -> HTTP edges
                    # 1b. Storage env vars -> DynamoDB/S3 edges (component-scoped)
                    http_clients = []
                    for container in containers:
                        for env in container.get("environment", []):
                            env_name = env.get("name", "")
                            env_value = (env.get("value", "") or "").strip()
                            m = _RE_BASEURL.match(env_name)
                            if m:
                                fragment = m.group(1).lower().replace("_", "-").strip("-")
                                svc_target = self._app_config.resolve_service_name(fragment)
                                http_clients.append(svc_target)

                            if (
                                env_value
                                and env_value in component_known
                                and len(env_value) > 3
                                and not env_value.startswith("http")
                                and not env_value.startswith("arn:")
                            ):
                                norm_name = component_known[env_value]
                                self._register_ecs_storage_access(component, norm_name, None)
                                logger.debug(
                                    "[cfn-live] ECS storage (env value match): %s -> %s (from %s=%s)",
                                    component,
                                    norm_name,
                                    env_name,
                                    env_value,
                                )

                    if http_clients:
                        self.ecs_http_clients.setdefault(component, [])
                        self.ecs_http_clients[component].extend(http_clients)

                    # 2. Task role IAM policies -> SQS and storage edges
                    task_role_arn = td.get("taskRoleArn", "")
                    if not task_role_arn:
                        logger.debug("[cfn-live] No task role for %s", svc_arn)
                        continue

                    role_name = task_role_arn.split("/")[-1]
                    logger.debug("[cfn-live] Scanning IAM role %s for SQS/storage access", role_name)

                    # Collect all policy documents (attached + inline)
                    policy_docs: list[dict] = []

                    # Attached managed policies
                    try:
                        for pol in _paged_items(self.iam, "list_attached_role_policies", "AttachedPolicies", RoleName=role_name):
                            pol_arn = pol["PolicyArn"]
                            pol_detail = self.iam.get_policy(PolicyArn=pol_arn)
                            version_id = pol_detail["Policy"]["DefaultVersionId"]
                            pol_version = self.iam.get_policy_version(
                                PolicyArn=pol_arn,
                                VersionId=version_id,
                            )
                            doc = pol_version["PolicyVersion"].get("Document", {})
                            if isinstance(doc, str):
                                doc = _json.loads(doc)
                            policy_docs.append(doc)
                    except Exception as e:
                        self._record_issue("ecs_attached_role_policies", role_name, e)

                    # Inline role policies
                    try:
                        for pol_name in _paged_items(self.iam, "list_role_policies", "PolicyNames", RoleName=role_name):
                            pol_inline = self.iam.get_role_policy(
                                RoleName=role_name,
                                PolicyName=pol_name,
                            )
                            doc = pol_inline.get("PolicyDocument", {})
                            if isinstance(doc, str):
                                doc = _json.loads(doc)
                            policy_docs.append(doc)
                    except Exception as e:
                        self._record_issue("ecs_inline_role_policies", role_name, e)

                    for doc in policy_docs:
                        for stmt in _statements(doc):
                            effect = stmt.get("Effect", "Allow")
                            if effect != "Allow":
                                continue

                            actions = stmt.get("Action", [])
                            if isinstance(actions, str):
                                actions = [actions]
                            actions_lower = {a.lower() for a in actions if isinstance(a, str)}

                            is_consumer = any(fnmatch.fnmatchcase("sqs:receivemessage", a) for a in actions_lower)
                            is_producer = any(fnmatch.fnmatchcase("sqs:sendmessage", a) for a in actions_lower)

                            dynamo_modes = _classify_storage_modes(
                                actions_lower=actions_lower,
                                service="dynamodb",
                                read_actions=_DYNAMO_READ_ACTIONS,
                                write_actions=_DYNAMO_WRITE_ACTIONS,
                            )
                            s3_modes = _classify_storage_modes(
                                actions_lower=actions_lower,
                                service="s3",
                                read_actions=_S3_READ_ACTIONS,
                                write_actions=_S3_WRITE_ACTIONS,
                            )

                            if not (is_consumer or is_producer or dynamo_modes or s3_modes):
                                continue

                            resources = stmt.get("Resource", [])
                            if isinstance(resources, str):
                                resources = [resources]

                            for res_arn in resources:
                                if not isinstance(res_arn, str):
                                    continue

                                # Wildcard resource: attach only to component-owned resources.
                                if res_arn == "*":
                                    if dynamo_modes:
                                        for table_name in owned_tables:
                                            self._register_ecs_storage_access(component, table_name, dynamo_modes)
                                            logger.debug(
                                                "[cfn-live] ECS DynamoDB (wildcard IAM): %s -> %s [%s]",
                                                component,
                                                table_name,
                                                "/".join(sorted(dynamo_modes)),
                                            )
                                    if s3_modes:
                                        for bucket_name in owned_buckets:
                                            self._register_ecs_storage_access(component, bucket_name, s3_modes)
                                            logger.debug(
                                                "[cfn-live] ECS S3 (wildcard IAM): %s -> %s [%s]",
                                                component,
                                                bucket_name,
                                                "/".join(sorted(s3_modes)),
                                            )
                                    continue

                                if ":sqs:" in res_arn:
                                    queue_name = res_arn.split(":")[-1]
                                    if not queue_name or queue_name == "*":
                                        continue
                                    if is_consumer:
                                        self.ecs_sqs_consumers.setdefault(component, [])
                                        self.ecs_sqs_consumers[component].append(queue_name)
                                        logger.debug("[cfn-live] ECS SQS consumer: %s <- %s", component, queue_name)
                                    if is_producer:
                                        self.ecs_sqs_producers.setdefault(component, [])
                                        self.ecs_sqs_producers[component].append(queue_name)
                                        logger.debug("[cfn-live] ECS SQS producer: %s -> %s", component, queue_name)
                                    continue

                                if ":dynamodb:" in res_arn and dynamo_modes:
                                    table_name = _extract_dynamodb_table_name(res_arn)
                                    if not table_name or table_name == "*":
                                        continue
                                    norm_name = component_known.get(table_name)
                                    if not norm_name or norm_name not in owned_tables:
                                        # Keep scope to tables created by the same microservice storage stack.
                                        continue
                                    self._register_ecs_storage_access(component, norm_name, dynamo_modes)
                                    logger.debug(
                                        "[cfn-live] ECS DynamoDB access: %s -> %s [%s]",
                                        component,
                                        norm_name,
                                        "/".join(sorted(dynamo_modes)),
                                    )
                                    continue

                                if ":s3:::" in res_arn and s3_modes:
                                    bucket_name = res_arn.split(":::")[-1].split("/")[0]
                                    if not bucket_name or bucket_name == "*":
                                        continue
                                    norm_name = component_known.get(bucket_name)
                                    if not norm_name or norm_name not in owned_buckets:
                                        continue
                                    self._register_ecs_storage_access(component, norm_name, s3_modes)
                                    logger.debug(
                                        "[cfn-live] ECS S3 access: %s -> %s [%s]",
                                        component,
                                        norm_name,
                                        "/".join(sorted(s3_modes)),
                                    )

            except Exception as e:
                self._record_issue("ecs_task_definition", svc_arn, e)

    def _enrich_lambda_iam(self) -> None:
        """
        For each Lambda function in our resource map, read its execution role IAM policies
        and extract DynamoDB/S3/SQS resource access edges.

        Scans:
          - dynamodb:GetItem / PutItem / Query / Scan / UpdateItem / DeleteItem → storage_access
          - s3:GetObject / PutObject / DeleteObject                              → storage_access

        Edges are stored in self.lambda_storage_access:
            lambda_fn_name → [table_name, bucket_name, ...]

        Resource ARNs like arn:aws:dynamodb:region:account:table/TableName
        are mapped to normalized names via self._queue_rawname_to_norm or direct extraction.
        """
        import json as _json

        _DYNAMO_READ_ACTIONS = frozenset({
            "dynamodb:getitem",
            "dynamodb:batchgetitem",
            "dynamodb:query",
            "dynamodb:scan",
            "dynamodb:transactgetitems",
            "dynamodb:describe*",
            "dynamodb:list*",
        })
        _DYNAMO_WRITE_ACTIONS = frozenset({
            "dynamodb:putitem",
            "dynamodb:updateitem",
            "dynamodb:deleteitem",
            "dynamodb:batchwriteitem",
            "dynamodb:transactwriteitems",
        })
        _S3_READ_ACTIONS = frozenset({
            "s3:getobject",
            "s3:getobjectversion",
            "s3:listbucket",
            "s3:headobject",
        })
        _S3_WRITE_ACTIONS = frozenset({
            "s3:putobject",
            "s3:deleteobject",
            "s3:deleteobjectversion",
            "s3:abortmultipartupload",
        })

        # Component-scoped storage lookup to avoid cross-component attribution.
        known_resources_by_component: dict[str, dict[str, str]] = {}
        component_tables_by_name: dict[str, set[str]] = {}
        component_buckets_by_name: dict[str, set[str]] = {}
        for component, resources in self.component_resources.items():
            aliases: dict[str, str] = {}
            owned_tables: set[str] = set()
            owned_buckets: set[str] = set()
            for res in resources:
                cfn_type = res.get("type", "")
                pid = res.get("physical_id", "")
                logical_id = res.get("logical_id", "")
                if cfn_type not in ("AWS::DynamoDB::Table", "AWS::S3::Bucket"):
                    continue
                name = _normalize_name(pid, cfn_type, logical_id)
                fragment = pid.split(":")[-1].split("/")[-1]
                aliases[pid] = name
                aliases[fragment] = name
                aliases[name] = name
                if cfn_type == "AWS::DynamoDB::Table":
                    owned_tables.add(name)
                elif cfn_type == "AWS::S3::Bucket":
                    owned_buckets.add(name)

            known_resources_by_component[component] = aliases
            component_tables_by_name[component] = owned_tables
            component_buckets_by_name[component] = owned_buckets

        lambda_entries: list[tuple[str, str]] = [
            (r["physical_id"], component)
            for component, resources in self.component_resources.items()
            for r in resources
            if r.get("type") == "AWS::Lambda::Function" and r.get("physical_id")
        ]

        logger.info("[cfn-live] Enriching IAM storage access for %d Lambda functions", len(lambda_entries))

        for fn_name, component in lambda_entries:
            try:
                fn_config = self.lmb.get_function_configuration(FunctionName=fn_name)
                role_arn = fn_config.get("Role", "")
                if not role_arn:
                    continue
                role_name = role_arn.split("/")[-1]

                # Collect all policy docs (attached + inline)
                policy_docs: list[dict] = []
                try:
                    for pol in _paged_items(self.iam, "list_attached_role_policies", "AttachedPolicies", RoleName=role_name):
                        pol_arn = pol["PolicyArn"]
                        # Skip AWS managed policies (they are too broad / not component-specific)
                        if pol_arn.startswith("arn:aws:iam::aws:policy/"):
                            continue
                        pol_detail = self.iam.get_policy(PolicyArn=pol_arn)
                        version_id = pol_detail["Policy"]["DefaultVersionId"]
                        pol_version = self.iam.get_policy_version(
                            PolicyArn=pol_arn, VersionId=version_id
                        )
                        doc = pol_version["PolicyVersion"].get("Document", {})
                        if isinstance(doc, str):
                            doc = _json.loads(doc)
                        policy_docs.append(doc)
                except Exception as e:
                    self._record_issue("lambda_attached_role_policies", fn_name, e)

                try:
                    for pol_name in _paged_items(self.iam, "list_role_policies", "PolicyNames", RoleName=role_name):
                        pol_inline = self.iam.get_role_policy(
                            RoleName=role_name, PolicyName=pol_name
                        )
                        doc = pol_inline.get("PolicyDocument", {})
                        if isinstance(doc, str):
                            doc = _json.loads(doc)
                        policy_docs.append(doc)
                except Exception as e:
                    self._record_issue("lambda_inline_role_policies", fn_name, e)

                # Normalize lambda name for edge source
                lambda_norm = _normalize_name(fn_name, "AWS::Lambda::Function", fn_name)
                component_known = known_resources_by_component.get(component, {})
                owned_tables = component_tables_by_name.get(component, set())
                owned_buckets = component_buckets_by_name.get(component, set())

                for doc in policy_docs:
                    for stmt in _statements(doc):
                        if stmt.get("Effect") != "Allow":
                            continue
                        actions = stmt.get("Action", [])
                        if isinstance(actions, str):
                            actions = [actions]
                        actions_lower = {a.lower() for a in actions}

                        dynamo_modes = _classify_storage_modes(
                            actions_lower=actions_lower,
                            service="dynamodb",
                            read_actions=_DYNAMO_READ_ACTIONS,
                            write_actions=_DYNAMO_WRITE_ACTIONS,
                        )
                        s3_modes = _classify_storage_modes(
                            actions_lower=actions_lower,
                            service="s3",
                            read_actions=_S3_READ_ACTIONS,
                            write_actions=_S3_WRITE_ACTIONS,
                        )
                        sqs_send = any(fnmatch.fnmatchcase("sqs:sendmessage", a) for a in actions_lower)
                        sqs_receive = any(fnmatch.fnmatchcase("sqs:receivemessage", a) for a in actions_lower)
                        if not (dynamo_modes or s3_modes or sqs_send or sqs_receive):
                            continue

                        resources = stmt.get("Resource", [])
                        if isinstance(resources, str):
                            resources = [resources]

                        for res_arn in resources:
                            if not isinstance(res_arn, str):
                                continue

                            if res_arn == "*":
                                if dynamo_modes:
                                    for table_name in owned_tables:
                                        self._register_lambda_storage_access(lambda_norm, table_name, dynamo_modes)
                                if s3_modes:
                                    for bucket_name in owned_buckets:
                                        self._register_lambda_storage_access(lambda_norm, bucket_name, s3_modes)
                                # No concrete SQS endpoint can be inferred from Resource:*.
                                continue

                            if ":sqs:" in res_arn and (sqs_send or sqs_receive):
                                queue = res_arn.split(":")[-1]
                                if not queue or "*" in queue or "?" in queue:
                                    continue
                                if sqs_send:
                                    self.lambda_sqs_producers.setdefault(lambda_norm, []).append(queue)
                                if sqs_receive:
                                    self.lambda_sqs_consumers.setdefault(lambda_norm, []).append(queue)
                                continue

                            if ":dynamodb:" in res_arn and dynamo_modes:
                                table_name = _extract_dynamodb_table_name(res_arn)
                                if not table_name or table_name == "*":
                                    continue
                                norm = component_known.get(table_name)
                                if not norm or norm not in owned_tables:
                                    continue
                                self._register_lambda_storage_access(lambda_norm, norm, dynamo_modes)
                                logger.debug(
                                    "[cfn-live] Lambda DynamoDB: %s -> %s [%s]",
                                    lambda_norm,
                                    norm,
                                    "/".join(sorted(dynamo_modes)),
                                )
                                continue

                            if ":s3:::" in res_arn and s3_modes:
                                bucket_name = res_arn.split(":::")[-1].split("/")[0]
                                if not bucket_name or bucket_name == "*":
                                    continue
                                norm = component_known.get(bucket_name)
                                if not norm or norm not in owned_buckets:
                                    continue
                                self._register_lambda_storage_access(lambda_norm, norm, s3_modes)
                                logger.debug(
                                    "[cfn-live] Lambda S3: %s -> %s [%s]",
                                    lambda_norm,
                                    norm,
                                    "/".join(sorted(s3_modes)),
                                )
                                continue

            except Exception as e:
                self._record_issue("lambda_iam", fn_name, e)

        total = sum(len(v) for v in self.lambda_storage_access.values())
        logger.info(
            "[cfn-live] Lambda IAM scan complete: %d lambdas with %d storage access edges",
            len(self.lambda_storage_access),
            total,
        )

    def _enrich_apigw_lambda(self) -> None:
        """
        Discover API Gateway → Lambda integration edges.

        Strategy: scan Lambda resource-based policies via lambda:get_policy().
        Each API Gateway integration adds a statement with
        ``"Condition": {"ArnLike": {"AWS:SourceArn": "arn:aws:execute-api:..."}}``
        to the Lambda's resource policy.

        This identifies an invocation permission, not proof of an active API
        integration or runtime traffic. Only known REST APIs are resolved.
        """
        import json as _json
        import re

        # Step 1: Build a lookup of API Gateway REST APIs we know about
        # physical_id for RestApi = the API ID (e.g. "abc123def4")
        apigw_resources: dict[str, tuple[str, str]] = {}  # api_id → (logical_id, component)
        for component, resources in self.component_resources.items():
            for res in resources:
                if res.get("type") == "AWS::ApiGateway::RestApi":
                    api_id = res["physical_id"]
                    logical_id = res.get("logical_id", api_id)
                    apigw_resources[api_id] = (logical_id, component)

        if not apigw_resources:
            logger.debug("[cfn-live] No API Gateway REST APIs found, skipping API GW enrichment")
            return

        # Step 2: For each Lambda in our resource_map, call get_policy()
        # to find API GW source ARN references
        lambda_entries = [(r["physical_id"], comp)
                          for comp, resources in self.component_resources.items()
                          for r in resources if r.get("type") == "AWS::Lambda::Function" and r.get("physical_id")]

        _APIGW_ARN_RE = re.compile(
            r"arn:aws:execute-api:[^:]+:[^:]+:([a-z0-9]+)/",
            re.IGNORECASE,
        )

        self.apigw_lambda_edges = []
        seen_edges: set[tuple[str, str]] = set()

        for fn_arn, component in lambda_entries:
            fn_name = _lambda_name(fn_arn)
            try:
                policy_resp = self.lmb.get_policy(FunctionName=fn_name)
                policy_str = policy_resp.get("Policy", "{}")
                policy = _json.loads(policy_str)

                for stmt in _statements(policy):
                    if stmt.get("Effect") != "Allow":
                        continue
                    principal = stmt.get("Principal", {})
                    services = principal.get("Service", []) if isinstance(principal, dict) else principal
                    if isinstance(services, str):
                        services = [services]
                    actions = stmt.get("Action", [])
                    if isinstance(actions, str):
                        actions = [actions]
                    if "apigateway.amazonaws.com" not in services or not any(
                        a.lower() in {"lambda:invokefunction", "lambda:*", "*"} for a in actions
                    ):
                        continue
                    condition = stmt.get("Condition", {})
                    # API Gateway adds: Condition.ArnLike["AWS:SourceArn"] = "arn:aws:execute-api:..."
                    source_arns = []
                    for cond_op in ("ArnLike", "ArnEquals"):
                        cond_block = condition.get(cond_op, {})
                        val = cond_block.get("AWS:SourceArn", cond_block.get("aws:SourceArn", ""))
                        if isinstance(val, str) and val:
                            source_arns.append(val)
                        elif isinstance(val, list):
                            source_arns.extend(val)

                    for source_arn in source_arns:
                        m = _APIGW_ARN_RE.match(source_arn)
                        if not m:
                            continue
                        api_id = m.group(1)
                        if api_id in apigw_resources:
                            apigw_logical, apigw_comp = apigw_resources[api_id]
                            # Normalize lambda name
                            lambda_norm = _normalize_name(fn_arn, "AWS::Lambda::Function", "")
                            edge_key = (apigw_logical, lambda_norm)
                            if edge_key not in seen_edges:
                                seen_edges.add(edge_key)
                                self.apigw_lambda_edges.append({
                                    "apigw_name": self.apigw_real_names.get(api_id, apigw_logical),
                                    "apigw_id": api_id,
                                    "lambda_name": lambda_norm,
                                    "component": component,
                                })

            except self.lmb.exceptions.ResourceNotFoundException:
                # Lambda has no resource policy → no API GW integration
                continue
            except Exception as e:
                self._record_issue("lambda_resource_policy", fn_name, e)

        logger.info(
            "[cfn-live] API GW enrichment complete: %d API GW -> Lambda edges",
            len(self.apigw_lambda_edges),
        )

    def _enrich_eventbridge_targets(self) -> None:
        """
        Discover EventBridge Rule → ECS / Lambda / SQS edges.

        For each AWS::Events::Rule in our resource map, call events:ListTargetsByRule.
        Supports both default-bus and custom-bus rules (physical_id = "bus|rule").

        Dispatches by target ARN service segment:
          - ":ecs:"     → eventbridge_ecs_edges
          - ":lambda:" or ":function:" → eventbridge_lambda_edges
          - ":sqs:"     → eventbridge_sqs_edges

        Fallback:
          If EventBridge targets do not return an SQS relation, scan SQS queue policies
          (sqs:GetQueueAttributes Policy) and infer Rule → Queue from aws:SourceArn.

        Edge format: rule_name → target_name
        """
        import json as _json

        def _build_rule_display_name(physical_id: str, logical_id: str) -> str:
            """Build a rule name that matches the inventory node naming style."""
            name = _normalize_name(physical_id, "AWS::Events::Rule", logical_id)
            env_tokens = ("-dev-", "-prod-", "-staging-", "-uat-")
            if logical_id and ("|" in physical_id or any(tok in name for tok in env_tokens)):
                kebab = _re.sub(r"(?<!^)(?=[A-Z])", "-", logical_id).lower()
                if kebab and len(kebab) > 3:
                    return kebab
            return name

        def _parse_rule_id(physical_id: str) -> tuple[Optional[str], str]:
            """Parse CFN rule physical id into (event_bus_name, rule_name)."""
            if "|" in physical_id:
                bus_name, rule_name = physical_id.split("|", 1)
                return bus_name, rule_name
            return None, physical_id

        def _parse_rule_from_source_arn(source_arn: str) -> tuple[Optional[str], Optional[str]]:
            """Extract (event_bus_name, rule_name) from EventBridge source ARN."""
            if ":rule/" not in source_arn:
                return None, None
            suffix = source_arn.split(":rule/", 1)[1]
            parts = suffix.split("/")
            if len(parts) == 1:
                return None, parts[0]
            return "/".join(parts[:-1]), parts[-1]

        def _is_events_principal(principal) -> bool:
            """Return True when policy principal is EventBridge service."""
            if isinstance(principal, str):
                return principal == "events.amazonaws.com"
            if isinstance(principal, dict):
                svc = principal.get("Service")
                if isinstance(svc, str):
                    return svc == "events.amazonaws.com"
                if isinstance(svc, list):
                    return "events.amazonaws.com" in svc
            return False

        def _extract_source_arns(stmt: dict) -> list[str]:
            """Extract aws:SourceArn values from common IAM condition operators."""
            cond = stmt.get("Condition", {})
            if not isinstance(cond, dict):
                return []

            out: list[str] = []
            for op in ("ArnEquals", "ArnLike", "StringEquals", "StringLike"):
                block = cond.get(op, {})
                if not isinstance(block, dict):
                    continue
                val = block.get("AWS:SourceArn", block.get("aws:SourceArn", ""))
                if isinstance(val, str) and val:
                    out.append(val)
                elif isinstance(val, list):
                    out.extend(v for v in val if isinstance(v, str) and v)
            return out

        # Build rule entries and lookups
        rule_entries: list[dict] = []
        rule_name_by_key: dict[tuple[str, str], str] = {}
        rule_component_by_key: dict[tuple[str, str], str] = {}
        for component, resources in self.component_resources.items():
            for res in resources:
                if res.get("type") == "AWS::Events::Rule":
                    physical_id = res.get("physical_id", "")
                    logical_id = res.get("logical_id", "")
                    if not physical_id:
                        continue

                    event_bus_name, rule_api_name = _parse_rule_id(physical_id)
                    rule_display_name = _build_rule_display_name(physical_id, logical_id)

                    rule_entries.append({
                        "physical_id": physical_id,
                        "rule_api_name": rule_api_name,
                        "event_bus_name": event_bus_name,
                        "rule_display_name": rule_display_name,
                        "component": component,
                    })

                    rule_key = (event_bus_name or "", rule_api_name)
                    rule_name_by_key[rule_key] = rule_display_name
                    rule_component_by_key[rule_key] = component

        if not rule_entries:
            logger.debug("[cfn-live] No EventBridge rules found, skipping EB enrichment")
            return

        # Build ECS component lookup: cluster+service → component_name
        ecs_components: dict[str, str] = {}
        for component, resources in self.component_resources.items():
            for res in resources:
                if res.get("type") == "AWS::ECS::Service":
                    ecs_components[component] = component
                    pid = res.get("physical_id", "")
                    if pid:
                        ecs_components[pid] = component

        # Also use resource_map which has ECS service ARNs
        for arn, comp in self.resource_map.items():
            if ":service/" in arn:
                ecs_components[arn] = comp
                svc_name = arn.split("/")[-1]
                ecs_components[svc_name] = comp

        seen: set[tuple[str, str]] = set()
        for entry in rule_entries:
            rule_api_name = entry["rule_api_name"]
            event_bus_name = entry["event_bus_name"]
            rule_display_name = entry["rule_display_name"]
            rule_component = entry["component"]

            try:
                params = {"Rule": rule_api_name}
                if event_bus_name:
                    params["EventBusName"] = event_bus_name

                for target in _paged_items(self.events, "list_targets_by_rule", "Targets", **params):
                    target_arn = target.get("Arn", "")

                    if ":ecs:" in target_arn:
                        # EventBridge → ECS
                        for ecs_key, ecs_comp in ecs_components.items():
                            if ecs_key in target_arn or target_arn.endswith(ecs_key):
                                edge_key = (rule_display_name, ecs_comp)
                                if edge_key not in seen:
                                    seen.add(edge_key)
                                    self.eventbridge_ecs_edges.append({
                                        "rule_name": rule_display_name,
                                        "ecs_component": ecs_comp,
                                        "component": rule_component,
                                    })
                                    logger.debug(
                                        "[cfn-live] EventBridge->ECS: %s -> %s",
                                        rule_display_name,
                                        ecs_comp,
                                    )
                                break

                    elif ":lambda:" in target_arn or ":function:" in target_arn:
                        # EventBridge → Lambda
                        fn_name = _lambda_name(target_arn)
                        norm_fn = _normalize_name(fn_name, "AWS::Lambda::Function", "")
                        edge_key = (rule_display_name, norm_fn)
                        if edge_key not in seen:
                            seen.add(edge_key)
                            self.eventbridge_lambda_edges.append({
                                "rule_name": rule_display_name,
                                "lambda_name": norm_fn,
                                "component": rule_component,
                            })
                            logger.debug(
                                "[cfn-live] EventBridge->Lambda: %s -> %s",
                                rule_display_name,
                                norm_fn,
                            )

                    elif ":sqs:" in target_arn:
                        # EventBridge → SQS
                        queue_name = target_arn.split(":")[-1]
                        edge_key = (rule_display_name, queue_name)
                        if edge_key not in seen:
                            seen.add(edge_key)
                            self.eventbridge_sqs_edges.append({
                                "rule_name": rule_display_name,
                                "queue_name": queue_name,
                                "component": rule_component,
                            })
                            logger.debug(
                                "[cfn-live] EventBridge->SQS: %s -> %s",
                                rule_display_name,
                                queue_name,
                            )

            except Exception as e:
                self._record_issue("list_targets_by_rule", entry["physical_id"], e)

        # Fallback: infer EventBridge Rule -> SQS from queue policies.
        queue_entries: list[tuple[str, str]] = [
            (res.get("physical_id", ""), component)
            for component, resources in self.component_resources.items()
            for res in resources
            if res.get("type") == "AWS::SQS::Queue" and res.get("physical_id", "").startswith("https://")
        ]

        for queue_url, queue_component in queue_entries:
            try:
                attrs = self.sqs.get_queue_attributes(
                    QueueUrl=queue_url,
                    AttributeNames=["QueueArn", "Policy"],
                ).get("Attributes", {})
                queue_arn = attrs.get("QueueArn", "")
                policy_raw = attrs.get("Policy", "")
                if not queue_arn or not policy_raw:
                    continue

                queue_name = queue_arn.split(":")[-1]
                policy_doc = _json.loads(policy_raw)

                for stmt in policy_doc.get("Statement", []):
                    if not isinstance(stmt, dict):
                        continue
                    if stmt.get("Effect", "Allow") != "Allow":
                        continue
                    if not _is_events_principal(stmt.get("Principal")):
                        continue

                    actions = stmt.get("Action", [])
                    if isinstance(actions, str):
                        actions = [actions]
                    actions_lower = {a.lower() for a in actions if isinstance(a, str)}
                    if "sqs:sendmessage" not in actions_lower and "sqs:*" not in actions_lower:
                        continue

                    for source_arn in _extract_source_arns(stmt):
                        source_bus, source_rule = _parse_rule_from_source_arn(source_arn)
                        if not source_rule:
                            continue

                        rule_key = (source_bus or "", source_rule)
                        source_rule_name = rule_name_by_key.get(rule_key)
                        source_rule_component = rule_component_by_key.get(rule_key, queue_component)
                        if not source_rule_name:
                            # Default fallback when rule is outside discovered stack scope.
                            source_rule_name = _normalize_name(source_rule, "AWS::Events::Rule", "")

                        edge_key = (source_rule_name, queue_name)
                        if edge_key in seen:
                            continue

                        seen.add(edge_key)
                        self.eventbridge_sqs_edges.append({
                            "rule_name": source_rule_name,
                            "queue_name": queue_name,
                            "component": source_rule_component,
                            "evidence": "sqs_resource_policy_permission",
                        })
                        logger.debug(
                            "[cfn-live] EventBridge->SQS (policy): %s -> %s",
                            source_rule_name,
                            queue_name,
                        )

            except Exception as e:
                self._record_issue("sqs_resource_policy", queue_url, e)

        logger.info(
            "[cfn-live] EventBridge enrichment complete: %d EB->ECS, %d EB->Lambda, %d EB->SQS edges",
            len(self.eventbridge_ecs_edges),
            len(self.eventbridge_lambda_edges),
            len(self.eventbridge_sqs_edges),
        )

    def _enrich_apigw_ecs(self) -> None:
        """
        Discover API Gateway → ECS edges using CFN resource membership only.

        Strategy (zero extra API calls):
          The SEND architecture routes: API GW → VPC Link → NLB → TargetGroup → ECS.
          All four resource types are in the same component's CFN stack:
            - AWS::ApiGateway::RestApi
            - AWS::ElasticLoadBalancingV2::TargetGroup  (proves ALB integration)
            - AWS::ECS::Service

          If a component has API GW + TargetGroup + ECS → emit apigw_integration edge.
          Fallback: if TargetGroup was excluded via resource_kinds.skip, fall back to
          requiring only API GW + ECS in the same component.

        No extra boto3 calls — all data comes from component_resources already built
        by _process_stack().
        """
        for component, resources in self.component_resources.items():
            # Collect API GW names for this component
            apigw_entries: list[str] = []
            has_target_group = False
            has_ecs = False

            for res in resources:
                cfn_type = res.get("type", "")
                if cfn_type in ("AWS::ApiGateway::RestApi", "AWS::ApiGatewayV2::Api"):
                    physical_id = res.get("physical_id", "")
                    logical_id = res.get("logical_id", physical_id)
                    name = self.apigw_real_names.get(physical_id, logical_id)
                    if name:
                        apigw_entries.append(name)
                elif cfn_type == "AWS::ElasticLoadBalancingV2::TargetGroup":
                    has_target_group = True
                elif cfn_type == "AWS::ECS::Service":
                    has_ecs = True

            if not apigw_entries or not has_ecs:
                continue

            # Co-location heuristic, not a proven integration chain. Preserve its
            # explicit evidence classification when serializing relationships.
            if has_target_group or has_ecs:
                proof = "TargetGroup verified" if has_target_group else "same-component fallback"
                for apigw_name in apigw_entries:
                    self.apigw_ecs_edges.append({
                        "apigw_name": apigw_name,
                        "ecs_component": component,
                        "component": component,
                    })
                    logger.debug(
                        "[cfn-live] API GW->ECS (%s): %s -> %s",
                        proof,
                        apigw_name,
                        component,
                    )

        logger.info(
            "[cfn-live] API GW->ECS enrichment complete: %d API GW -> ECS edges",
            len(self.apigw_ecs_edges),
        )

    def _enrich_sns_subscriptions(self) -> None:
        """
        Discover SNS Topic → SQS / Lambda fan-out edges.

        For each AWS::SNS::Topic in our resource map, call
        sns:ListSubscriptionsByTopic and emit an sns_subscription edge for
        each SQS or Lambda endpoint found.

        Requires the SNS client (self.sns) initialised in __init__.
        """
        sns_entries: list[tuple[str, str]] = [
            (r["physical_id"], component)
            for component, resources in self.component_resources.items()
            for r in resources
            if r.get("type") == "AWS::SNS::Topic" and r.get("physical_id")
        ]

        if not sns_entries:
            logger.debug("[cfn-live] No SNS topics found, skipping SNS enrichment")
            return

        logger.info("[cfn-live] Enriching SNS subscriptions for %d topics", len(sns_entries))

        for topic_arn, component in sns_entries:
            try:
                norm_topic = _normalize_name(topic_arn, "AWS::SNS::Topic", "")
                for sub in _paged_items(self.sns, "list_subscriptions_by_topic", "Subscriptions", TopicArn=topic_arn):
                    protocol = sub.get("Protocol", "")
                    endpoint = sub.get("Endpoint", "")
                    if not endpoint or endpoint == "PendingConfirmation":
                        continue
                    if sub.get("SubscriptionArn") in {"PendingConfirmation", "Deleted"}:
                        continue

                    if protocol == "sqs" and ":sqs:" in endpoint:
                        queue_name = endpoint.split(":")[-1]
                        self.sns_edges.append({
                            "source_topic": norm_topic,
                            "target": queue_name,
                            "target_type": "sqs",
                            "component": component,
                        })
                        logger.debug(
                            "[cfn-live] SNS->SQS: %s -> %s",
                            norm_topic,
                            queue_name,
                        )
                    elif protocol == "lambda" and ":function:" in endpoint:
                        fn_name = _lambda_name(endpoint)
                        norm_fn = _normalize_name(fn_name, "AWS::Lambda::Function", "")
                        self.sns_edges.append({
                            "source_topic": norm_topic,
                            "target": norm_fn,
                            "target_type": "lambda",
                            "component": component,
                        })
                        logger.debug(
                            "[cfn-live] SNS->Lambda: %s -> %s",
                            norm_topic,
                            norm_fn,
                        )

            except Exception as e:
                self._record_issue("list_subscriptions_by_topic", topic_arn, e)

        logger.info(
            "[cfn-live] SNS enrichment complete: %d SNS fan-out edges",
            len(self.sns_edges),
        )
