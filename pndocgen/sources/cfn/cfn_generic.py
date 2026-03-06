"""
pn-docgen - Generic CFN Analyzer Plugin

Parses one or more CloudFormation templates without assuming a SEND-specific
split (``storage.yml`` + ``microservice.yml``).

This plugin extracts the same high-level signals used by the resolver:
  - owned SQS queues
  - owned S3 buckets
  - Lambda function names
  - SQS producer/consumer intent from IAM policy statements
  - HTTP clients from configured ECS env-var naming conventions
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from pndocgen.engine.core.config import AppConfig, get_app_config
from pndocgen.engine.core.models import ComponentAnalysis
from pndocgen.sources.cfn.base import SourceAnalyzer
from pndocgen.sources.cfn.yaml_loader import load_yaml_cfn, resolve_cfn_sub

logger = logging.getLogger(__name__)

_IAM_TYPES = {"AWS::IAM::ManagedPolicy", "AWS::IAM::Policy", "AWS::IAM::Role"}
_RE_QUEUE_LIST = re.compile(r"SPRING_CLOUD_FUNCTIONROUTER_QUEUES_LIST=([^\s'\"\\n]+)")


def _build_env_regexes(internal_domains: list[str]) -> list[re.Pattern]:
    """Build BASEURL matching regexes from configured internal domains."""
    patterns: list[re.Pattern] = []
    for domain in internal_domains:
        escaped = re.escape(domain).replace(r"\$\{", r"\$\{").replace(r"\}", r"\}")
        for prefix in (
            r"ContainerEnvEntry\d+:\s*[!]Sub\s+['\"]?",
            r"ContainerEnvEntry\d+:\s*['\"]?",
        ):
            patterns.append(
                re.compile(
                    prefix
                    + r"PN_[A-Z]+_([A-Z_]+?)(?:BASE_URL|BASEURL)="
                    + r"http://"
                    + escaped
                    + r"[^'\"\\n]*['\"]?",
                    re.IGNORECASE,
                )
            )
    return patterns


class CfnGenericAnalyzer(SourceAnalyzer):
    """Generic static analyzer over configured CloudFormation template files."""

    name = "cfn-generic"

    def __init__(
        self,
        app_config: Optional[AppConfig] = None,
        template_files: Optional[list[Path]] = None,
    ) -> None:
        """Initialize the analyzer.

        Args:
            app_config: Optional pre-loaded application configuration.
            template_files: Optional explicit template list. When omitted, values
                are read from ``discovery.cfn_analyzer.template_files`` with
                fallback to legacy ``discovery.cfn_paths`` entries.
        """
        self._app_config = app_config or get_app_config()

        if template_files is not None:
            self._template_files = [Path(p) for p in template_files]
        else:
            configured = [
                str(x).strip()
                for x in self._app_config.discovery.cfn_analyzer.template_files
                if str(x).strip()
            ]
            if configured:
                self._template_files = [Path(x) for x in configured]
            else:
                # Backward-compatible fallback: use legacy cfn_paths values.
                cfn_paths = self._app_config.discovery.cfn_paths
                ordered_unique: list[Path] = []
                seen: set[str] = set()
                for value in cfn_paths.values():
                    path_str = str(value).strip()
                    if not path_str or path_str in seen:
                        continue
                    seen.add(path_str)
                    ordered_unique.append(Path(path_str))
                self._template_files = ordered_unique

        ecs_cfg = self._app_config.discovery.ecs_dependencies
        self._env_regexes = _build_env_regexes(ecs_cfg.internal_domains)

    def can_run(self, repo_path: Path) -> bool:
        """Return ``True`` when at least one configured template exists."""
        return any((repo_path / rel_path).exists() for rel_path in self._template_files)

    def analyze(self, repo_path: Path, analysis: ComponentAnalysis) -> None:
        project_name = analysis.name
        existing_templates = [
            repo_path / rel_path
            for rel_path in self._template_files
            if (repo_path / rel_path).exists()
        ]

        if not existing_templates:
            logger.info("[cfn-generic] %s: no configured templates found", project_name)
            return

        for template_path in existing_templates:
            content = template_path.read_text()
            data = load_yaml_cfn(template_path)
            resources = data.get("Resources", {}) or {}

            self._parse_resources(resources, project_name, analysis)
            self._parse_iam(resources, content, analysis)
            self._parse_env_entries(content, analysis)

        logger.info(
            "[cfn-generic] %s: %d template(s), %d producers, %d consumers, %d queues, %d lambdas",
            project_name,
            len(existing_templates),
            len(analysis.producer_queues),
            len(analysis.consumer_queues),
            len(analysis.owned_queues),
            len(analysis.lambdas),
        )

    def _parse_resources(self, resources: dict, project_name: str, analysis: ComponentAnalysis) -> None:
        """Extract owned resources from CloudFormation ``Resources`` entries."""
        for resource_name, resource in resources.items():
            if not isinstance(resource, dict):
                continue
            resource_type = resource.get("Type", "")
            props = resource.get("Properties", {}) or {}

            if resource_type == "AWS::SQS::Queue":
                raw = props.get("QueueName", resource_name)
                analysis.owned_queues.append(resolve_cfn_sub(str(raw), project_name))

            elif resource_type == "AWS::S3::Bucket":
                raw = props.get("BucketName", resource_name)
                analysis.s3_buckets.append(resolve_cfn_sub(str(raw), project_name))

            elif resource_type == "AWS::Lambda::Function":
                raw = props.get("FunctionName", resource_name)
                analysis.lambdas.append(resolve_cfn_sub(str(raw), project_name))

            elif resource_type == "AWS::CloudFormation::Stack":
                # Compatibility with common nested queue fragments.
                template_url = str(props.get("TemplateURL", ""))
                params = props.get("Parameters", {}) or {}
                if "sqs-queue" in template_url and "QueueName" in params:
                    queue_name = resolve_cfn_sub(str(params.get("QueueName", "")), project_name)
                    if queue_name:
                        analysis.owned_queues.append(queue_name)

    def _parse_iam(self, resources: dict, content: str, analysis: ComponentAnalysis) -> None:
        """Extract SQS producer/consumer intent from IAM policy statements."""
        for resource in resources.values():
            if not isinstance(resource, dict):
                continue
            if resource.get("Type") not in _IAM_TYPES:
                continue

            props = resource.get("Properties", {}) or {}
            policy_docs: list[dict] = []
            if isinstance(props.get("PolicyDocument"), dict):
                policy_docs.append(props["PolicyDocument"])
            for pol in props.get("Policies", []) or []:
                if isinstance(pol, dict) and isinstance(pol.get("PolicyDocument"), dict):
                    policy_docs.append(pol["PolicyDocument"])

            for doc in policy_docs:
                for stmt in doc.get("Statement", []) or []:
                    if not isinstance(stmt, dict):
                        continue
                    actions = stmt.get("Action", [])
                    if isinstance(actions, str):
                        actions = [actions]
                    if not isinstance(actions, list):
                        continue

                    actions_l = [a.lower() for a in actions if isinstance(a, str)]
                    has_send = any(a == "sqs:sendmessage" or a == "sqs:*" or a == "*" for a in actions_l)
                    has_receive = any(a == "sqs:receivemessage" or a == "sqs:*" or a == "*" for a in actions_l)
                    if not (has_send or has_receive):
                        continue

                    res_list = stmt.get("Resource", [])
                    if isinstance(res_list, str):
                        res_list = [res_list]
                    if not isinstance(res_list, list):
                        res_list = [res_list]

                    for res in res_list:
                        ref = self._queue_ref_from_resource(res)
                        if not ref:
                            continue
                        if has_send:
                            analysis.producer_queues.append(ref)
                        if has_receive:
                            analysis.consumer_queues.append(ref)

        # Optional fallback for Spring queue list env var in template text.
        for match in _RE_QUEUE_LIST.finditer(content):
            for qv in match.group(1).split(","):
                qv = qv.strip().strip("'\"")
                inner = re.sub(r"^\$\{|\}$", "", qv)
                if inner:
                    ref = f"<{inner}>"
                    if ref not in analysis.consumer_queues:
                        analysis.consumer_queues.append(ref)

    def _parse_env_entries(self, content: str, analysis: ComponentAnalysis) -> None:
        """Extract HTTP client dependencies from BASEURL-like env entries."""
        for regex in self._env_regexes:
            for match in regex.finditer(content):
                fragment = match.group(1).lower().replace("_", "-").strip("-")
                svc = self._app_config.resolve_service_name(fragment)
                if svc and svc != analysis.name and svc not in analysis.http_clients:
                    analysis.http_clients.append(svc)

    @staticmethod
    def _queue_ref_from_resource(res) -> Optional[str]:
        """Build a symbolic queue reference from an IAM ``Resource`` value."""
        if not res:
            return None
        if isinstance(res, str):
            if ":sqs:" in res:
                return res.split(":")[-1]
            name = res.replace("ARN", "").replace("Arn", "").replace("URL", "")
            return f"<{name}>" if name else None
        if isinstance(res, dict):
            ref = res.get("Ref") or res.get("Fn::GetAtt", [None])[0]
            if ref:
                name = str(ref).replace("ARN", "").replace("Arn", "").replace("URL", "")
                return f"<{name}>"
        return None
