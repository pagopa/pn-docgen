"""
pn-docgen - AWS Discoverer

Discovers AWS resources using boto3 and returns a list of InventoryNode.
Ported from inventory_pn_core.sh — same resources, Python/boto3 instead of AWS CLI.

Usage:
    discoverer = AWSDiscoverer(profile="sso_pn-core-dev", region="eu-south-1")
    nodes = discoverer.discover()
"""

import re
import boto3
import logging
from typing import Optional
from botocore.exceptions import ClientError, NoCredentialsError

from pndocgen.engine.core.config import AppConfig, get_app_config
from pndocgen.engine.core.models import InventoryNode

logger = logging.getLogger(__name__)


def _extract_account_env(
    profile: str, profile_regex: str = ""
) -> tuple[str, str]:
    """
    Extract account and env from AWS profile name.

    The *profile_regex* should contain two capture groups for
    (account, environment).  When empty/unset, the profile name itself
    is returned as account with ``"unknown"`` as env.
    """
    if profile_regex:
        match = re.match(profile_regex, profile)
        if match:
            return match.group(1), match.group(2)
    # Fallback: use profile name as account
    return profile, "unknown"


def _tags_to_dict(tag_list: list) -> dict:
    """Convert AWS tag list [{Key: ..., Value: ...}] to plain dict."""
    return {t["Key"]: t["Value"] for t in (tag_list or [])}


class AWSDiscoverer:
    """
    Discovers all relevant AWS resources for pn-docgen.
    Covers: ECS, Lambda, SQS, SNS, DynamoDB, EventBridge, Kinesis, Step Functions, API Gateway.
    """

    def __init__(self, profile: str, region: str, name_prefix: str = "",
                 discovery_strategy=None, app_config: Optional[AppConfig] = None):
        self._app_config = app_config or get_app_config()
        self.profile = profile
        self.region = region
        # Use explicit name_prefix if provided, otherwise fall back to config
        self.name_prefix = name_prefix or self._app_config.project.prefix
        self.account, self.env = _extract_account_env(
            profile, self._app_config.project.aws_profile_regex
        )
        self._discovery_strategy = discovery_strategy

        session = boto3.Session(profile_name=profile, region_name=region)
        self._ecs = session.client("ecs")
        self._lambda = session.client("lambda")
        self._sqs = session.client("sqs")
        self._sns = session.client("sns")
        self._dynamo = session.client("dynamodb")
        self._events = session.client("events")
        self._kinesis = session.client("kinesis")
        self._sfn = session.client("stepfunctions")
        self._apigw = session.client("apigateway")
        self._tagging = session.client("resourcegroupstaggingapi")

    def discover(self) -> list[InventoryNode]:
        """Run full discovery and return all nodes."""
        logger.info(
            "Starting discovery for account=%s env=%s region=%s",
            self.account,
            self.env,
            self.region,
        )
        nodes = []
        nodes.extend(self._discover_ecs())
        nodes.extend(self._discover_lambda())
        nodes.extend(self._discover_sqs())
        nodes.extend(self._discover_sns())
        nodes.extend(self._discover_dynamodb())
        nodes.extend(self._discover_eventbridge())
        nodes.extend(self._discover_kinesis())
        nodes.extend(self._discover_stepfunctions())
        logger.info("Discovery complete: %d resources found", len(nodes))

        # Annotate nodes with component name via strategy
        if self._discovery_strategy:
            assigned = 0
            for node in nodes:
                component = self._discovery_strategy.assign_component(node)
                if component:
                    node.metadata["component"] = component
                    assigned += 1
            logger.info(
                "  Strategy '%s': %d/%d nodes assigned",
                self._discovery_strategy.name,
                assigned,
                len(nodes),
            )

        return nodes

    # ------------------------------------------------------------------
    # ECS Services
    # ------------------------------------------------------------------
    def _discover_ecs(self) -> list[InventoryNode]:
        nodes = []
        try:
            clusters_resp = self._ecs.list_clusters()
            for cluster_arn in clusters_resp.get("clusterArns", []):
                cluster_name = cluster_arn.split("/")[-1]
                paginator = self._ecs.get_paginator("list_services")
                for page in paginator.paginate(cluster=cluster_arn):
                    service_arns = page.get("serviceArns", [])
                    if not service_arns:
                        continue
                    # Batch describe (max 10 per call)
                    for i in range(0, len(service_arns), 10):
                        batch = service_arns[i:i+10]
                        resp = self._ecs.describe_services(cluster=cluster_arn, services=batch)
                        for svc in resp.get("services", []):
                            name = svc["serviceName"]
                            if not name.startswith(self.name_prefix):
                                continue
                            tags = _tags_to_dict(svc.get("tags", []))
                            nodes.append(InventoryNode(
                                id=svc["serviceArn"],
                                name=name,
                                resource_type="ecs_service",
                                account=self.account,
                                region=self.region,
                                tags=tags,
                                metadata={
                                    "cluster": cluster_name,
                                    "desired_count": svc.get("desiredCount"),
                                    "running_count": svc.get("runningCount"),
                                    "task_definition": svc.get("taskDefinition"),
                                }
                            ))
        except ClientError as e:
            logger.warning("ECS discovery error: %s", e)
        logger.info("  ECS: %d services", len(nodes))
        return nodes

    # ------------------------------------------------------------------
    # Lambda Functions
    # ------------------------------------------------------------------
    def _discover_lambda(self) -> list[InventoryNode]:
        nodes = []
        try:
            paginator = self._lambda.get_paginator("list_functions")
            for page in paginator.paginate():
                for fn in page.get("Functions", []):
                    name = fn["FunctionName"]
                    if not name.startswith(self.name_prefix):
                        continue
                    # Fetch tags separately
                    try:
                        tags_resp = self._lambda.list_tags(Resource=fn["FunctionArn"])
                        tags = tags_resp.get("Tags", {})
                    except ClientError:
                        tags = {}
                    nodes.append(InventoryNode(
                        id=fn["FunctionArn"],
                        name=name,
                        resource_type="lambda",
                        account=self.account,
                        region=self.region,
                        tags=tags,
                        metadata={
                            "runtime": fn.get("Runtime"),
                            "memory": fn.get("MemorySize"),
                            "timeout": fn.get("Timeout"),
                        }
                    ))
        except ClientError as e:
            logger.warning("Lambda discovery error: %s", e)
        logger.info("  Lambda: %d functions", len(nodes))
        return nodes

    # ------------------------------------------------------------------
    # SQS Queues
    # ------------------------------------------------------------------
    def _discover_sqs(self) -> list[InventoryNode]:
        nodes = []
        try:
            resp = self._sqs.list_queues(QueueNamePrefix=self.name_prefix, MaxResults=1000)
            for url in resp.get("QueueUrls", []):
                name = url.split("/")[-1]
                try:
                    attrs = self._sqs.get_queue_attributes(
                        QueueUrl=url,
                        AttributeNames=["QueueArn", "ApproximateNumberOfMessages"]
                    )["Attributes"]
                    tags_resp = self._sqs.list_queue_tags(QueueUrl=url)
                    tags = tags_resp.get("Tags", {})
                except ClientError:
                    attrs, tags = {}, {}
                nodes.append(InventoryNode(
                    id=attrs.get("QueueArn", url),
                    name=name,
                    resource_type="sqs",
                    account=self.account,
                    region=self.region,
                    tags=tags,
                    metadata={"url": url, "message_count": attrs.get("ApproximateNumberOfMessages")}
                ))
        except ClientError as e:
            logger.warning("SQS discovery error: %s", e)
        logger.info("  SQS: %d queues", len(nodes))
        return nodes

    # ------------------------------------------------------------------
    # SNS Topics
    # ------------------------------------------------------------------
    def _discover_sns(self) -> list[InventoryNode]:
        nodes = []
        try:
            paginator = self._sns.get_paginator("list_topics")
            for page in paginator.paginate():
                for topic in page.get("Topics", []):
                    arn = topic["TopicArn"]
                    name = arn.split(":")[-1]
                    if not name.startswith(self.name_prefix):
                        continue
                    try:
                        tags_resp = self._sns.list_tags_for_resource(ResourceArn=arn)
                        tags = _tags_to_dict(tags_resp.get("Tags", []))
                    except ClientError:
                        tags = {}
                    nodes.append(InventoryNode(
                        id=arn,
                        name=name,
                        resource_type="sns",
                        account=self.account,
                        region=self.region,
                        tags=tags,
                    ))
        except ClientError as e:
            logger.warning("SNS discovery error: %s", e)
        logger.info("  SNS: %d topics", len(nodes))
        return nodes

    # ------------------------------------------------------------------
    # DynamoDB Tables
    # ------------------------------------------------------------------
    def _discover_dynamodb(self) -> list[InventoryNode]:
        nodes = []
        try:
            paginator = self._dynamo.get_paginator("list_tables")
            for page in paginator.paginate():
                for table_name in page.get("TableNames", []):
                    if not table_name.startswith(self.name_prefix):
                        continue
                    try:
                        desc = self._dynamo.describe_table(TableName=table_name)["Table"]
                        arn = desc["TableArn"]
                        tags_resp = self._dynamo.list_tags_of_resource(ResourceArn=arn)
                        tags = _tags_to_dict(tags_resp.get("Tags", []))
                    except ClientError:
                        arn = table_name
                        tags = {}
                    nodes.append(InventoryNode(
                        id=arn,
                        name=table_name,
                        resource_type="dynamodb",
                        account=self.account,
                        region=self.region,
                        tags=tags,
                    ))
        except ClientError as e:
            logger.warning("DynamoDB discovery error: %s", e)
        logger.info("  DynamoDB: %d tables", len(nodes))
        return nodes

    # ------------------------------------------------------------------
    # EventBridge Rules
    # ------------------------------------------------------------------
    def _discover_eventbridge(self) -> list[InventoryNode]:
        nodes = []
        try:
            paginator = self._events.get_paginator("list_rules")
            for page in paginator.paginate():
                for rule in page.get("Rules", []):
                    name = rule["Name"]
                    if not name.startswith(self.name_prefix):
                        continue
                    nodes.append(InventoryNode(
                        id=rule["Arn"],
                        name=name,
                        resource_type="eventbridge_rule",
                        account=self.account,
                        region=self.region,
                        tags={},
                        metadata={
                            "event_bus": rule.get("EventBusName", "default"),
                            "schedule": rule.get("ScheduleExpression"),
                            "state": rule.get("State"),
                        }
                    ))
        except ClientError as e:
            logger.warning("EventBridge discovery error: %s", e)
        logger.info("  EventBridge: %d rules", len(nodes))
        return nodes

    # ------------------------------------------------------------------
    # Kinesis Streams
    # ------------------------------------------------------------------
    def _discover_kinesis(self) -> list[InventoryNode]:
        nodes = []
        try:
            resp = self._kinesis.list_streams(Limit=100)
            for stream_name in resp.get("StreamNames", []):
                if not stream_name.startswith(self.name_prefix):
                    continue
                try:
                    desc = self._kinesis.describe_stream_summary(StreamName=stream_name)
                    arn = desc["StreamDescriptionSummary"]["StreamARN"]
                except ClientError:
                    arn = stream_name
                nodes.append(InventoryNode(
                    id=arn,
                    name=stream_name,
                    resource_type="kinesis",
                    account=self.account,
                    region=self.region,
                    tags={},
                ))
        except ClientError as e:
            logger.warning("Kinesis discovery error: %s", e)
        logger.info("  Kinesis: %d streams", len(nodes))
        return nodes

    # ------------------------------------------------------------------
    # Step Functions
    # ------------------------------------------------------------------
    def _discover_stepfunctions(self) -> list[InventoryNode]:
        nodes = []
        try:
            paginator = self._sfn.get_paginator("list_state_machines")
            for page in paginator.paginate():
                for sm in page.get("stateMachines", []):
                    name = sm["name"]
                    if not name.startswith(self.name_prefix):
                        continue
                    nodes.append(InventoryNode(
                        id=sm["stateMachineArn"],
                        name=name,
                        resource_type="step_function",
                        account=self.account,
                        region=self.region,
                        tags={},
                    ))
        except ClientError as e:
            logger.warning("Step Functions discovery error: %s", e)
        logger.info("  Step Functions: %d state machines", len(nodes))
        return nodes
