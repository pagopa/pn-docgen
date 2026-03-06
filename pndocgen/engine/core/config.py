"""
pndocgen config loader.

Reads pndocgen.yaml (or pndocgen.yml) from CWD or an explicit path and
builds an AppConfig that centralises **all** project-specific values.

The tool is project-agnostic: every project-specific constant that was
previously hardcoded now lives in the YAML under ``project`` and
``discovery`` sections.  When those sections are absent the dataclasses
fall back to empty/generic defaults so the tool still works (it just
won't apply any project-specific heuristics).

YAML format (abridged)
-----------------------
project:
  prefix: "pn-"
  aws_profile_regex: 'sso_pn-([a-z]+)-([a-z]+)'
  java_group_id: "com.example"
  description: "Service Map"

discovery:
  ecs_dependencies:
    env_regex: 'PN_[A-Z]+_([A-Z_]+?)(?:BASE_URL|BASEURL)'
    internal_domains:
      - '${ApplicationLoadBalancerDomain}'
      - 'alb.confidential.pn.internal'
    service_alias_map:
      delivery: "pn-delivery"
      ...

resource_kinds:
  skip:
    - aws_wafv2_webacl
  node:
    - aws_lambda_layerversion

pattern: auto
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pndocgen.engine.core.models import (
    ResourceKind,
    ResourceMeta,
    RenderHint,
    SemanticRole,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Sentinel ResourceMeta objects used when the YAML only specifies a
# kind, not the full metadata.  The generator will use sensible defaults.
# ──────────────────────────────────────────────────────────────────────
_KIND_DEFAULTS: dict[str, ResourceMeta] = {
    "node": ResourceMeta(
        kind=ResourceKind.NODE,
        role=SemanticRole.UNKNOWN,
        render_hint=RenderHint.BOX,
    ),
    "edge": ResourceMeta(
        kind=ResourceKind.EDGE,
        role=SemanticRole.RELATION,
        render_hint=RenderHint.DASHED_ARROW,
    ),
    "skip": ResourceMeta(
        kind=ResourceKind.SKIP,
        role=SemanticRole.UNKNOWN,
        render_hint=RenderHint.NONE,
    ),
}


# ──────────────────────────────────────────────────────────────────────
# NEW — De-hardcoded config dataclasses
# ──────────────────────────────────────────────────────────────────────

@dataclass
class ProjectConfig:
    """Project-level identity.  Empty defaults → project-agnostic."""

    prefix: str = ""
    """Resource name prefix (e.g. ``"pn-"``).
    Used for label stripping, name resolution fallback, CLI defaults."""

    aws_profile_regex: str = ""
    """Regex applied to the AWS SSO profile name to extract
    (account_type, environment) groups.
    Example: ``'sso_pn-([a-z]+)-([a-z]+)(?:-readonly)?'``"""

    java_group_id: str = ""
    """Maven groupId prefix for the POM analyzer (e.g. ``"com.example"``)."""

    description: str = "AWS Architecture Diagram Generator"
    """Human-readable project title shown in CLI help and diagram titles."""


@dataclass
class EcsDependencyConfig:
    """Controls how ECS env-var scanning discovers HTTP client edges."""

    env_regex: str = ""
    """Regex (with one capture group) to extract a service fragment from
    container environment variable names.
    Example: ``'PN_[A-Z]+_([A-Z_]+?)(?:BASE_URL|BASEURL)'``"""

    internal_domains: list[str] = field(default_factory=list)
    """Internal ALB / DNS patterns that indicate an intra-platform HTTP
    call.  Used to build the BASEURL matching regexes.
    Example: ``['${ApplicationLoadBalancerDomain}',
               'alb.confidential.pn.internal']``"""

    service_alias_map: dict[str, str] = field(default_factory=dict)
    """Maps env-var fragments (lowercase, hyphens) to canonical service
    names.  When the fragment isn't in the map, the fallback is
    ``f"{project.prefix}{fragment}"``.
    Example: ``{"delivery": "pn-delivery",
                "deliverypush": "pn-delivery-push"}``"""


@dataclass
class DiscoveryConfig:
    """Settings for the AWS/CFN discovery phase."""

    @dataclass
    class CfnAnalyzerConfig:
        """Configuration for static CFN repository analyzers.

        mode:
            - generic: use one generic analyzer over configured template files
            - legacy: use split analyzers over configured cfn_paths
            - hybrid: run both legacy and generic analyzers
        """

        mode: str = "generic"
        template_files: list[str] = field(default_factory=list)

    cfn_paths: dict[str, str] = field(default_factory=dict)
    """Well-known CFN template paths relative to the repo root.
    Example: ``{"microservice": "scripts/aws/cfn/microservice.yml",
                "storage": "scripts/aws/cfn/storage.yml"}``"""

    cfn_analyzer: CfnAnalyzerConfig = field(default_factory=CfnAnalyzerConfig)

    ecs_dependencies: EcsDependencyConfig = field(
        default_factory=EcsDependencyConfig,
    )


@dataclass
class EdgeFilterConfig:
    """Controls which edge types are rendered in diagrams.

    Logic (applied in ``d2_generator.py``):
    - If ``include`` is non-empty → render ONLY the listed edge types.
    - Else if ``exclude`` is non-empty → render ALL edge types EXCEPT listed.
    - Else → render ALL edge types (default behaviour).

    Valid edge type names:
        sqs_trigger, sqs_consumer, sqs_producer,
        http_call, storage_access,
        apigw_integration, eventbridge_trigger,
        dynamodb_stream, sns_subscription
    """

    include: list[str] = field(default_factory=list)
    """If non-empty, ONLY these edge types are rendered."""

    exclude: list[str] = field(default_factory=list)
    """Edge types to suppress (ignored when ``include`` is non-empty)."""

    def is_allowed(self, edge_type: str) -> bool:
        """Return True if this edge_type should be rendered."""
        if self.include:
            return edge_type in self.include
        if self.exclude:
            return edge_type not in self.exclude
        return True


@dataclass
class RenderConfig:
    """Settings for diagram rendering."""

    title: str = ""
    """Override for the L2 service-map title.
    Falls back to ``f"{project.description} Service Map"``."""

    label_strip_prefix: bool = True
    """Whether to strip ``project.prefix`` from node labels."""


# ──────────────────────────────────────────────────────────────────────
# AppConfig — single top-level object aggregating everything
# ──────────────────────────────────────────────────────────────────────

@dataclass
class AppConfig:
    """Unified configuration loaded from ``pndocgen.yaml``.

    Every consumer in the codebase should obtain settings from here
    instead of using hardcoded constants.

    Use the class method :meth:`from_yaml` to build an instance::

        cfg = AppConfig.from_yaml()          # searches CWD
        cfg = AppConfig.from_yaml(some_path) # explicit path
    """

    project: ProjectConfig = field(default_factory=ProjectConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    edge_filter: EdgeFilterConfig = field(default_factory=EdgeFilterConfig)

    # Existing config sections (loaded by legacy helpers too)
    resource_kind_overrides: dict[str, ResourceMeta] = field(
        default_factory=dict,
    )
    pattern: str = "auto"

    # The path from which this config was loaded (None → defaults only)
    _source_path: Optional[Path] = field(default=None, repr=False)

    # ── Factory ──────────────────────────────────────────────────────

    @classmethod
    def from_yaml(cls, config_path: Optional[Path] = None) -> "AppConfig":
        """Build an ``AppConfig`` from a YAML file.

        If *config_path* is ``None`` the usual CWD search is used.
        Missing sections produce empty/default sub-configs — the tool
        stays fully functional, just without project-specific heuristics.
        """
        try:
            import yaml
        except ImportError:
            logger.debug("PyYAML not installed — using all defaults")
            return cls()

        path = config_path or _find_config()
        if not path or not path.exists():
            logger.debug("No pndocgen.yaml found — using all defaults")
            return cls()

        with path.open() as f:
            data = yaml.safe_load(f) or {}

        # ── project ──────────────────────────────────────────────────
        proj_raw = data.get("project") or {}
        project = ProjectConfig(
            prefix=str(proj_raw.get("prefix", "")).strip(),
            aws_profile_regex=str(proj_raw.get("aws_profile_regex", "")).strip(),
            java_group_id=str(proj_raw.get("java_group_id", "")).strip(),
            description=str(
                proj_raw.get("description", ProjectConfig.description)
            ).strip(),
        )

        # ── discovery ────────────────────────────────────────────────
        disc_raw = data.get("discovery") or {}
        cfn_paths = disc_raw.get("cfn_paths") or {}
        cfn_analyzer_raw = disc_raw.get("cfn_analyzer") or {}
        ecs_raw = disc_raw.get("ecs_dependencies") or {}

        analyzer_mode = str(cfn_analyzer_raw.get("mode", "generic")).strip().lower()
        if analyzer_mode not in {"legacy", "generic", "hybrid"}:
            logger.warning(
                "Unknown discovery.cfn_analyzer.mode '%s' in config, using 'generic'",
                analyzer_mode,
            )
            analyzer_mode = "generic"

        raw_template_files = cfn_analyzer_raw.get("template_files") or []
        if isinstance(raw_template_files, str):
            raw_template_files = [raw_template_files]
        elif not isinstance(raw_template_files, list):
            raw_template_files = []

        cfn_analyzer = DiscoveryConfig.CfnAnalyzerConfig(
            mode=analyzer_mode,
            template_files=[str(x) for x in raw_template_files],
        )

        ecs_deps = EcsDependencyConfig(
            env_regex=str(ecs_raw.get("env_regex", "")).strip(),
            internal_domains=list(ecs_raw.get("internal_domains") or []),
            service_alias_map={
                str(k): str(v)
                for k, v in (ecs_raw.get("service_alias_map") or {}).items()
            },
        )
        discovery = DiscoveryConfig(
            cfn_paths={str(k): str(v) for k, v in cfn_paths.items()},
            cfn_analyzer=cfn_analyzer,
            ecs_dependencies=ecs_deps,
        )

        # ── render ───────────────────────────────────────────────────
        render_raw = data.get("render") or {}
        render = RenderConfig(
            title=str(render_raw.get("title", "")).strip(),
            label_strip_prefix=bool(
                render_raw.get("label_strip_prefix", True)
            ),
        )

        # ── edge_types (filtering) ───────────────────────────────────
        et_raw = data.get("edge_types") or {}
        edge_filter = EdgeFilterConfig(
            include=[str(x) for x in (et_raw.get("include") or [])],
            exclude=[str(x) for x in (et_raw.get("exclude") or [])],
        )

        # ── resource_kinds (existing section) ────────────────────────
        rk_overrides = _parse_resource_kinds(data.get("resource_kinds"))

        # ── pattern (existing section) ───────────────────────────────
        raw_pattern = str(data.get("pattern", "auto")).strip().lower()
        pattern = raw_pattern if raw_pattern in VALID_PATTERNS else "auto"

        cfg = cls(
            project=project,
            discovery=discovery,
            render=render,
            edge_filter=edge_filter,
            resource_kind_overrides=rk_overrides,
            pattern=pattern,
            _source_path=path,
        )
        logger.info("Loaded config from %s", path)
        return cfg

    # ── Convenience helpers ──────────────────────────────────────────

    @property
    def service_map_title(self) -> str:
        """Title string for L2 service-map diagrams."""
        if self.render.title:
            return self.render.title
        return f"{self.project.description} Service Map"

    def resolve_service_name(self, fragment: str) -> str:
        """Map an env-var fragment to a canonical service name.

        Lookup order:
        1. ``discovery.ecs_dependencies.service_alias_map``
        2. ``f"{project.prefix}{fragment}"``
        3. *fragment* as-is (when prefix is empty)
        """
        alias_map = self.discovery.ecs_dependencies.service_alias_map
        if fragment in alias_map:
            return alias_map[fragment]
        if self.project.prefix:
            return f"{self.project.prefix}{fragment}"
        return fragment


# ──────────────────────────────────────────────────────────────────────
# Module-level singleton & convenience accessor
# ──────────────────────────────────────────────────────────────────────

_cached_config: Optional[AppConfig] = None


def get_app_config(
    config_path: Optional[Path] = None,
    *,
    force_reload: bool = False,
) -> AppConfig:
    """Return a module-level cached :class:`AppConfig`.

    The first call loads from disk (or *config_path*); subsequent calls
    return the cached instance unless *force_reload* is ``True``.
    """
    global _cached_config
    if _cached_config is None or force_reload:
        _cached_config = AppConfig.from_yaml(config_path)
    return _cached_config


# ──────────────────────────────────────────────────────────────────────
# Valid diagram patterns
# ──────────────────────────────────────────────────────────────────────
VALID_PATTERNS = frozenset({"auto", "ecs_microservice", "lambda_microservice"})


# ──────────────────────────────────────────────────────────────────────
# Legacy helpers — kept for backward compatibility
# Consumers should migrate to AppConfig in Steps 2/3.
# ──────────────────────────────────────────────────────────────────────

def load_resource_kind_overrides(
    config_path: Optional[Path] = None,
) -> dict[str, ResourceMeta]:
    """
    Load resource_kinds overrides from pndocgen.yaml.

    Args:
        config_path: explicit path to the YAML file. If None, searches
                     for pndocgen.yaml / pndocgen.yml in CWD.

    Returns:
        dict mapping resource_type → ResourceMeta. Empty dict if no
        config file is found or the resource_kinds section is absent.
    """
    cfg = get_app_config(config_path)
    return cfg.resource_kind_overrides


def load_pattern(
    config_path: Optional[Path] = None,
) -> str:
    """
    Load the diagram pattern from pndocgen.yaml.

    The pattern controls which L3 template is used and how the resolver
    distributes resources across clusters.

    YAML key:
        pattern: auto | ecs_microservice | lambda_microservice

    Priority order (highest first):
        1. Explicit config_path argument
        2. pndocgen.yaml / pndocgen.yml in CWD
        3. Default: "auto"

    Returns:
        One of "auto", "ecs_microservice", "lambda_microservice".
        Falls back to "auto" for unknown values.
    """
    cfg = get_app_config(config_path)
    return cfg.pattern


# ──────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────

def _parse_resource_kinds(
    rk_section: dict | None,
) -> dict[str, ResourceMeta]:
    """Parse the ``resource_kinds`` YAML section into overrides."""
    overrides: dict[str, ResourceMeta] = {}
    if not rk_section:
        return overrides
    for kind_str, types in rk_section.items():
        meta = _KIND_DEFAULTS.get(kind_str.lower())
        if meta is None:
            continue  # ignore unknown kind values
        for resource_type in (types or []):
            if isinstance(resource_type, str):
                overrides[resource_type.strip()] = meta
    return overrides


def _find_config() -> Optional[Path]:
    """Search for pndocgen.yaml or pndocgen.yml in CWD."""
    for name in ("pndocgen.yaml", "pndocgen.yml"):
        p = Path.cwd() / name
        if p.exists():
            return p
    return None
