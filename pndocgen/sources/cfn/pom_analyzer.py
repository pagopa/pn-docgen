"""
pn-docgen - POM Analyzer Plugin (optional)

Reads pom.xml and extracts:
  - Internal pn-* dependencies (configured Maven group)
  - Presence of spring-cloud-stream-binder-sqs (signals SQS usage)

This is an OPTIONAL SourceAnalyzer plugin.
It can be excluded if pom.xml is not available or not relevant.
"""

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from pndocgen.engine.core.config import AppConfig, get_app_config
from pndocgen.engine.core.models import ComponentAnalysis
from pndocgen.sources.cfn.base import SourceAnalyzer

logger = logging.getLogger(__name__)

POM_FILE = Path("pom.xml")


class PomAnalyzer(SourceAnalyzer):
    """
    Optional plugin: reads pom.xml to extract internal pn-* dependencies
    and detect SQS usage via spring-cloud-stream-binder-sqs.
    """

    name = "pom"

    def __init__(self, app_config: Optional[AppConfig] = None):
        self._app_config = app_config or get_app_config()

    def can_run(self, repo_path: Path) -> bool:
        return (repo_path / POM_FILE).exists()

    def analyze(self, repo_path: Path, analysis: ComponentAnalysis) -> None:
        path = repo_path / POM_FILE
        try:
            tree = ET.parse(path)
            root = tree.getroot()

            # Handle Maven namespace (e.g. {http://maven.apache.org/POM/4.0.0})
            ns = ""
            if root.tag.startswith("{"):
                ns = root.tag.split("}")[0] + "}"

            for dep in root.iter(f"{ns}dependency"):
                group = dep.find(f"{ns}groupId")
                artifact = dep.find(f"{ns}artifactId")
                if group is None or artifact is None:
                    continue

                group_text = group.text or ""
                artifact_text = artifact.text or ""

                # Internal dependencies (groupId from config, prefix from config)
                _java_gid = self._app_config.project.java_group_id
                _prefix = self._app_config.project.prefix
                if _java_gid and _java_gid in group_text:
                    if (_prefix and artifact_text.startswith(_prefix)) and artifact_text != analysis.name:
                        analysis.internal_deps.append(artifact_text)

                # SQS usage signal
                if artifact_text in (
                    "spring-cloud-stream-binder-sqs",
                    "spring-cloud-starter-aws-messaging",
                ):
                    analysis.metadata["uses_sqs"] = True
                    logger.debug("  [pom] SQS usage detected via %s", artifact_text)

        except Exception as e:
            logger.warning("[pom] Cannot parse %s: %s", path, e)
            return

        logger.info(
            "[pom] %s: %d internal deps, uses_sqs=%s",
            analysis.name,
            len(analysis.internal_deps),
            analysis.metadata.get("uses_sqs", False),
        )
