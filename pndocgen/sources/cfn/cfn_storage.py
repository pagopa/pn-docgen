"""
pn-docgen - CFN Storage Analyzer Plugin

Reads scripts/aws/cfn/storage.yml and extracts:
  - SQS queues owned by the microservice
  - S3 buckets owned by the microservice

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

DEFAULT_STORAGE_FILE = Path("scripts/aws/cfn/storage.yml")


class CfnStorageAnalyzer(SourceAnalyzer):
    """
    Plugin: reads storage.yml to extract owned SQS queues and S3 buckets.
    """

    name = "cfn-storage"

    def __init__(
        self,
        app_config: Optional[AppConfig] = None,
        storage_file: Optional[Path] = None,
    ) -> None:
        """Initialize the analyzer.

        Args:
            app_config: Optional pre-loaded application configuration.
            storage_file: Optional explicit storage template path relative to
                repository root. When omitted, uses ``discovery.cfn_paths.storage``
                or the built-in default.
        """
        self._app_config = app_config or get_app_config()
        configured = self._app_config.discovery.cfn_paths.get("storage", "")
        configured_path = Path(configured) if configured else DEFAULT_STORAGE_FILE
        self._storage_file = storage_file or configured_path

    def can_run(self, repo_path: Path) -> bool:
        """Return ``True`` when the configured storage template exists."""
        return (repo_path / self._storage_file).exists()

    def analyze(self, repo_path: Path, analysis: ComponentAnalysis) -> None:
        path = repo_path / self._storage_file
        content = path.read_text()
        data = load_yaml_cfn(path)
        resources = data.get("Resources", {}) or {}
        project_name = analysis.name

        for resource_name, resource in resources.items():
            if not isinstance(resource, dict):
                continue
            resource_type = resource.get("Type", "")
            props = resource.get("Properties", {}) or {}

            # CloudFormation nested stack wrapping sqs-queue.yaml fragment
            if resource_type == "AWS::CloudFormation::Stack":
                template_url = str(props.get("TemplateURL", ""))
                params = props.get("Parameters", {}) or {}
                if "sqs-queue" in template_url:
                    raw = params.get("QueueName", "")
                    queue_name = resolve_cfn_sub(str(raw), project_name)
                    if queue_name:
                        analysis.owned_queues.append(queue_name)
                        logger.debug("  [cfn-storage] owned queue: %s", queue_name)

            # Native SQS Queue resource
            elif resource_type == "AWS::SQS::Queue":
                raw = props.get("QueueName", resource_name)
                queue_name = resolve_cfn_sub(str(raw), project_name)
                analysis.owned_queues.append(queue_name)
                logger.debug("  [cfn-storage] owned queue: %s", queue_name)

            # Native S3 Bucket resource
            elif resource_type == "AWS::S3::Bucket":
                raw = props.get("BucketName", resource_name)
                bucket_name = resolve_cfn_sub(str(raw), project_name)
                analysis.s3_buckets.append(bucket_name)
                logger.debug("  [cfn-storage] s3 bucket: %s", bucket_name)

        # Regex fallback: catch QueueName: !Sub '...' patterns not parsed by YAML
        for match in re.finditer(
            r"QueueName:\s*[!]Sub\s+['\"]?([^'\"\n]+)['\"]?", content
        ):
            resolved = resolve_cfn_sub(match.group(1), project_name)
            if resolved and resolved not in analysis.owned_queues:
                analysis.owned_queues.append(resolved)

        logger.info(
            "[cfn-storage] %s: %d queues, %d buckets",
            project_name,
            len(analysis.owned_queues),
            len(analysis.s3_buckets),
        )
