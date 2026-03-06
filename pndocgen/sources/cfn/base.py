"""
pn-docgen - Source Analyzer Plugin Interface

Every source analyzer (CFN storage, CFN microservice, pom.xml, etc.)
implements this interface. The main CfnAnalyzer orchestrates them.

To add a new plugin:
  1. Create a class that inherits from SourceAnalyzer
  2. Implement the `analyze` method
  3. Register it in CfnAnalyzer.DEFAULT_PLUGINS or pass it via `plugins=`
"""

from abc import ABC, abstractmethod
from pathlib import Path

from pndocgen.engine.core.models import ComponentAnalysis


class SourceAnalyzer(ABC):
    """
    Plugin interface for static source analyzers.
    Each plugin reads one type of source file and enriches a ComponentAnalysis.
    """

    # Human-readable name shown in logs and CLI output
    name: str = "unnamed"

    @abstractmethod
    def can_run(self, repo_path: Path) -> bool:
        """
        Return True if this plugin can run on the given repo.
        Used to skip plugins when their required files are missing.
        """
        ...

    @abstractmethod
    def analyze(self, repo_path: Path, analysis: ComponentAnalysis) -> None:
        """
        Enrich `analysis` in-place by reading files from `repo_path`.
        Must not raise exceptions — log warnings and return gracefully.
        """
        ...
