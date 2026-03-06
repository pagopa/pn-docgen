"""
pn-docgen - CFN Microservice Analyzer Plugin

Reads scripts/aws/cfn/microservice.yml and extracts:
  - SQS producer/consumer relationships (from IAM policy statements)
  - HTTP client dependencies (from ContainerEnvEntry *BASEURL env vars)
  - Lambda function names

This is a SourceAnalyzer plugin — it can be enabled/disabled independently.
"""

import re
import logging
from pathlib import Path
from typing import Optional

from pndocgen.engine.core.config import AppConfig, get_app_config
from pndocgen.engine.core.models import ComponentAnalysis
from pndocgen.sources.cfn.base import SourceAnalyzer
from pndocgen.sources.cfn.yaml_loader import load_yaml_cfn, resolve_cfn_sub

logger = logging.getLogger(__name__)

DEFAULT_MICROSERVICE_FILE = Path("scripts/aws/cfn/microservice.yml")


def _build_env_regexes(
    internal_domains: list[str],
) -> list[re.Pattern]:
    """Build BASEURL-matching regexes from the configured internal domains.

    Each domain produces a regex that matches
    ``ContainerEnvEntry<N>: ... PN_*_<fragment>BASEURL=http://<domain>...``
    """
    patterns: list[re.Pattern] = []
    for domain in internal_domains:
        # Escape for regex, but allow ${...} CFN references
        escaped = re.escape(domain).replace(r"\$\{", r"\$\{").replace(r"\}", r"\}")
        # Two variants: !Sub style and plain string style
        for prefix in (
            r"ContainerEnvEntry\d+:\s*[!]Sub\s+['\"]?",
            r"ContainerEnvEntry\d+:\s*['\"]?",
        ):
            pat = re.compile(
                prefix
                + r"PN_[A-Z]+_([A-Z_]+?)(?:BASE_URL|BASEURL)="
                + r"http://" + escaped + r"[^'\"\\n]*['\"]?",
                re.IGNORECASE,
            )
            patterns.append(pat)
    return patterns

# Regex: SPRING_CLOUD_FUNCTIONROUTER_QUEUES_LIST consumer queues
_RE_QUEUE_LIST = re.compile(
    r"SPRING_CLOUD_FUNCTIONROUTER_QUEUES_LIST=([^\s'\"\\n]+)"
)

# IAM resource types
_IAM_TYPES = {"AWS::IAM::ManagedPolicy", "AWS::IAM::Policy", "AWS::IAM::Role"}


class CfnMicroserviceAnalyzer(SourceAnalyzer):
    """
    Plugin: reads microservice.yml to extract:
    - SQS producer/consumer (IAM policy)
    - HTTP clients (ContainerEnvEntry *BASEURL)
    - Lambda function names
    """

    name = "cfn-microservice"

    def __init__(self, app_config: Optional[AppConfig] = None) -> None:
        """Initialize the analyzer.

        Args:
            app_config: Optional pre-loaded application configuration.
        """
        self._app_config = app_config or get_app_config()
        ecs_cfg = self._app_config.discovery.ecs_dependencies
        configured = self._app_config.discovery.cfn_paths.get("microservice", "")
        self._microservice_file = Path(configured) if configured else DEFAULT_MICROSERVICE_FILE
        # Build regexes dynamically from configured internal domains
        self._env_regexes = _build_env_regexes(ecs_cfg.internal_domains)

    def can_run(self, repo_path: Path) -> bool:
        return (repo_path / self._microservice_file).exists()

    def analyze(self, repo_path: Path, analysis: ComponentAnalysis) -> None:
        path = repo_path / self._microservice_file
        content = path.read_text()
        data = load_yaml_cfn(path)
        resources = data.get("Resources", {}) or {}
        project_name = analysis.name

        # 1. IAM policy → producer/consumer queues
        self._parse_iam(resources, content, analysis)

        # 2. ContainerEnvEntry → HTTP clients
        self._parse_env_entries(content, analysis)

        # 3. Lambda function names
        self._parse_lambdas(resources, project_name, analysis)

        logger.info(
            f"[cfn-microservice] {project_name}: "
            f"{len(analysis.producer_queues)} producers, "
            f"{len(analysis.consumer_queues)} consumers, "
            f"{len(analysis.http_clients)} HTTP clients, "
            f"{len(analysis.lambdas)} lambdas"
        )

    # ------------------------------------------------------------------

    def _parse_iam(self, resources: dict, content: str, analysis: ComponentAnalysis) -> None:
        """
        Walk IAM policy statements.
        Resources after YAML parse are strings (CFN param names) like
        'DeliveryPushInputsQueueARN' — we use them as symbolic queue refs.
        """
        for resource_name, resource in resources.items():
            if not isinstance(resource, dict):
                continue
            if resource.get("Type") not in _IAM_TYPES:
                continue

            props = resource.get("Properties", {}) or {}
            policy_docs = []
            if "PolicyDocument" in props:
                policy_docs.append(props["PolicyDocument"])
            for pol in props.get("Policies", []) or []:
                if isinstance(pol, dict) and "PolicyDocument" in pol:
                    policy_docs.append(pol["PolicyDocument"])

            for doc in policy_docs:
                if not isinstance(doc, dict):
                    continue
                for stmt in doc.get("Statement", []) or []:
                    if not isinstance(stmt, dict):
                        continue
                    actions = stmt.get("Action", [])
                    if isinstance(actions, str):
                        actions = [actions]
                    if not isinstance(actions, list):
                        continue

                    has_send = any("sqs:SendMessage" in a for a in actions)
                    has_receive = any("sqs:ReceiveMessage" in a for a in actions)

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
                        # Determine direction based on actions
                        # If both send+receive → it's a full-access queue (owned)
                        if has_send and has_receive:
                            # Likely an owned queue with full access
                            analysis.producer_queues.append(ref)
                            analysis.consumer_queues.append(ref)
                        elif has_send:
                            analysis.producer_queues.append(ref)
                        else:
                            analysis.consumer_queues.append(ref)

        # Fallback: SPRING_CLOUD_FUNCTIONROUTER_QUEUES_LIST → consumer queues
        for match in _RE_QUEUE_LIST.finditer(content):
            for qv in match.group(1).split(","):
                qv = qv.strip().strip("'\"")
                inner = re.sub(r"^\$\{|\}$", "", qv)
                if inner:
                    ref = f"<{inner}>"
                    if ref not in analysis.consumer_queues:
                        analysis.consumer_queues.append(ref)

    def _parse_env_entries(self, content: str, analysis: ComponentAnalysis) -> None:
        """
        Extract HTTP client dependencies from ContainerEnvEntry *BASEURL lines.
        Uses dynamically built regexes from configured internal_domains.
        """
        for regex in self._env_regexes:
            for match in regex.finditer(content):
                fragment = match.group(1).lower().replace("_", "-").strip("-")
                svc = self._fragment_to_service(fragment)
                if svc and svc != analysis.name:
                    if svc not in analysis.http_clients:
                        analysis.http_clients.append(svc)
                    logger.debug("  [cfn-micro] HTTP client: %s", svc)

    def _parse_lambdas(self, resources: dict, project_name: str, analysis: ComponentAnalysis) -> None:
        """Extract Lambda function names from Resources."""
        for resource_name, resource in resources.items():
            if not isinstance(resource, dict):
                continue
            if resource.get("Type") != "AWS::Lambda::Function":
                continue
            props = resource.get("Properties", {}) or {}
            raw = props.get("FunctionName", resource_name)
            fn_name = resolve_cfn_sub(str(raw), project_name)
            analysis.lambdas.append(fn_name)

    # ------------------------------------------------------------------

    @staticmethod
    def _queue_ref_from_resource(res: object) -> Optional[str]:
        """
        Extract a symbolic queue reference from an IAM Resource value.
        After CFN YAML parse, Resources are strings (CFN param names).
        Returns a human-readable symbolic name like 'DeliveryPushInputsQueue'.
        """
        if not res:
            return None
        if isinstance(res, str):
            # Strip common suffixes to get a clean name
            name = res.replace("ARN", "").replace("Arn", "").replace("URL", "")
            return f"<{name}>" if name else None
        if isinstance(res, dict):
            # !Ref or !GetAtt — use the logical name
            ref = res.get("Ref") or res.get("Fn::GetAtt", [None])[0]
            if ref:
                name = str(ref).replace("ARN", "").replace("Arn", "").replace("URL", "")
                return f"<{name}>"
        return None

    def _fragment_to_service(self, fragment: str) -> Optional[str]:
        """Map an env var fragment to a canonical service name via config."""
        # Try without trailing dash first
        clean = fragment.strip("-")
        return self._app_config.resolve_service_name(clean)
