"""
pn-docgen - CFN Analyzer (Plugin Orchestrator)

Orchestrates SourceAnalyzer plugins to produce a ComponentAnalysis
from a CloudFormation-based microservice repository.

Default plugin set is selected by ``discovery.cfn_analyzer.mode``:
  - generic → CfnGenericAnalyzer over configured template files
  - legacy  → split analyzers (storage + microservice paths)
  - hybrid  → both generic and split analyzers

Optional plugins (must be explicitly added):
  - PomAnalyzer          → pom.xml (internal deps, SQS usage signal)

Usage:
    # Default mode-driven plugins
    analyzer = CfnAnalyzer(repo_path=Path("repo_link/pn-delivery"))
    analysis = analyzer.analyze()

    # With optional pom plugin
    from pndocgen.sources.cfn.pom_analyzer import PomAnalyzer
    analyzer = CfnAnalyzer(
        repo_path=Path("repo_link/pn-delivery"),
        extra_plugins=[PomAnalyzer()]
    )
    analysis = analyzer.analyze()

    # Custom plugin set (override defaults)
    analyzer = CfnAnalyzer(
        repo_path=Path("repo_link/pn-delivery"),
        plugins=[CfnStorageAnalyzer()]  # only storage, skip microservice
    )
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from pndocgen.engine.core.config import AppConfig, get_app_config
from pndocgen.engine.core.models import ComponentAnalysis
from pndocgen.sources.cfn.base import SourceAnalyzer
from pndocgen.sources.cfn.cfn_generic import CfnGenericAnalyzer
from pndocgen.sources.cfn.cfn_storage import CfnStorageAnalyzer
from pndocgen.sources.cfn.cfn_micro import CfnMicroserviceAnalyzer

logger = logging.getLogger(__name__)


class CfnAnalyzer:
    """
    Orchestrates SourceAnalyzer plugins to analyze a microservice repo.

    Plugin lifecycle:
      1. For each plugin, call can_run(repo_path) — skip if False
      2. Call analyze(repo_path, analysis) to enrich ComponentAnalysis in-place
      3. Deduplicate all list fields
    """

    def __init__(
        self,
        repo_path: Path,
        plugins: Optional[list[SourceAnalyzer]] = None,
        extra_plugins: Optional[list[SourceAnalyzer]] = None,
        app_config: Optional[AppConfig] = None,
    ) -> None:
        """
        Args:
            repo_path: Path to the microservice repository root.
            plugins: Optional explicit plugin list. When provided, it replaces
                mode-driven default plugin selection.
            extra_plugins: Optional plugins appended to the selected defaults.
            app_config: Optional pre-loaded application configuration.
        """
        self.repo_path = Path(repo_path)
        self.service_name = self.repo_path.name
        self._app_config = app_config or get_app_config()

        if plugins is not None:
            self._plugins = plugins
        else:
            mode = self._app_config.discovery.cfn_analyzer.mode
            legacy_plugins = [
                CfnStorageAnalyzer(app_config=self._app_config),
                CfnMicroserviceAnalyzer(app_config=self._app_config),
            ]
            generic_plugins = [
                CfnGenericAnalyzer(app_config=self._app_config),
            ]

            if mode == "generic":
                self._plugins = generic_plugins
            elif mode == "hybrid":
                self._plugins = legacy_plugins + generic_plugins
            else:
                self._plugins = legacy_plugins

            if extra_plugins:
                self._plugins.extend(extra_plugins)

    def analyze(self) -> ComponentAnalysis:
        """Run all active plugins and return a ComponentAnalysis."""
        logger.info("Analyzing %s at %s", self.service_name, self.repo_path)
        analysis = ComponentAnalysis(name=self.service_name)

        for plugin in self._plugins:
            if not plugin.can_run(self.repo_path):
                logger.info("  [%s] skipped (required files not found)", plugin.name)
                continue
            logger.info("  [%s] running...", plugin.name)
            try:
                plugin.analyze(self.repo_path, analysis)
            except Exception as e:
                logger.warning("  [%s] error: %s", plugin.name, e)

        # Deduplicate all list fields
        analysis.owned_queues = sorted(set(analysis.owned_queues))
        analysis.producer_queues = sorted(set(analysis.producer_queues))
        analysis.consumer_queues = sorted(set(analysis.consumer_queues))
        analysis.http_clients = sorted(set(analysis.http_clients))
        analysis.lambdas = sorted(set(analysis.lambdas))
        analysis.s3_buckets = sorted(set(analysis.s3_buckets))
        analysis.internal_deps = sorted(set(analysis.internal_deps))

        logger.info(
            "Analysis complete for %s: %d owned queues, %d producers, %d consumers, %d HTTP clients, %d lambdas",
            self.service_name,
            len(analysis.owned_queues),
            len(analysis.producer_queues),
            len(analysis.consumer_queues),
            len(analysis.http_clients),
            len(analysis.lambdas),
        )
        return analysis
