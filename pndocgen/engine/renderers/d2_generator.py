"""
pn-docgen - D2 Diagram Generator

Takes a ComponentGraph and generates D2 diagram files using Jinja2 templates.
Supports L3 (microservice detail) diagrams.
- ecs_microservice pattern uses the v74 golden layout (5-column, box-level edges)
- other patterns use the generic L3_microservice template
"""

import logging
import subprocess
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

from pndocgen.engine.core.config import get_app_config
from pndocgen.engine.core.models import ComponentGraph, Component, get_resource_meta

logger = logging.getLogger(__name__)


# Icons directory bundled inside the package: pndocgen/engine/d2/icons/aws/
# Populated by running:  python scripts/download_aws_icons.py
_ICONS_DIR = Path(__file__).parent.parent / "d2" / "icons" / "aws"


def _local_icon(name: str) -> str | None:
    """Return absolute path to a bundled PNG icon, or None if not downloaded yet."""
    p = _ICONS_DIR / f"{name}.png"
    return str(p) if p.exists() else None


# Maps logical icon names (ResourceMeta.icon) to bundled SVG paths.
# Run  scripts/download_aws_icons.py  to populate the icons directory.
_ICON_URL: dict[str, str | None] = {k: _local_icon(k) for k in [
    "aws-lambda", "aws-ecs", "aws-step-functions", "aws-glue", "aws-batch",
    "aws-dynamodb", "aws-s3", "aws-elasticache", "aws-rds", "aws-opensearch", "aws-efs",
    "aws-sqs", "aws-sns", "aws-kinesis", "aws-eventbridge",
    "aws-api-gateway", "aws-alb", "aws-nlb", "aws-cloudfront",
    "aws-waf", "aws-cognito",
]}

# Fallback: omit icon entirely when SVGs are not present (render still works, just no image).
_FALLBACK_ICON: str | None = None

_missing = [k for k, v in _ICON_URL.items() if not v]
if _missing:
    logger.warning(
        "Icons not found (%d/%d). Run: python scripts/download_aws_icons.py",
        len(_missing), len(_ICON_URL),
    )

# Node size by cluster role
SIZE_MAP = {
    # v74 golden layout cluster names
    "apigw":    (60, 60),
    "rules":    (60, 60),
    "queues":   (60, 60),
    "ecs":      (110, 110),
    "lambdas":  (60, 60),
    "dynamodb": (60, 60),
    "external": (60, 60),
    # legacy cluster names (non-ecs patterns)
    "compute":   (60, 60),
    "inputs":    (50, 50),
    "messaging": (50, 50),
    "storage":   (60, 60),
    "security":  (40, 40),
    "misc":      (50, 50),
}

# Maps cluster name → D2 column path prefix used in the v74 template
_V74_BOX_PATH: dict[str, str] = {
    "apigw":         "col1.apigw",
    "rules":         "col1.rules",
    "queues":        "col2.queues",
    "ecs":           "col3.ecs",
    "lambdas":       "col3.lambdas",
    "dynamodb":      "col4.dynamodb",
    "storage":       "col4.storage",
    "output_queues": "col4.output_queues",  # producer queues (ECS writes to)
    "external":      "col5.external",
}

# Box-level edge color coding for v74 ecs_microservice layout
# key: (src_cluster, dst_cluster) → (stroke_color, label, stroke_dash)
_V74_BOX_EDGE_STYLE: dict[tuple[str, str], tuple[str, str, bool]] = {
    # Ingress → compute
    ("apigw",         "lambdas"):       ("#1565C0", "integrates",  False),
    ("apigw",         "ecs"):           ("#1565C0", "integrates",  False),
    # EventBridge rules → compute / queues
    ("rules",         "lambdas"):       ("#F57C00", "triggers",    False),
    ("rules",         "ecs"):           ("#F57C00", "triggers",    False),
    ("rules",         "queues"):        ("#F57C00", "routes",      False),
    ("rules",         "output_queues"): ("#F57C00", "routes",      False),
    # Consumer queues → compute (SQS triggers)
    ("queues",        "ecs"):           ("#1565C0", "consumes",    False),
    ("queues",        "lambdas"):       ("#F57C00", "triggers",    False),
    # Compute → storage
    ("ecs",           "dynamodb"):      ("#2E7D32", "reads/writes",False),
    ("ecs",           "storage"):       ("#2E7D32", "reads/writes",False),
    # Compute → producer queues (right side, same column as storage)
    ("ecs",           "output_queues"): ("#1565C0", "produces",    False),
    ("lambdas",       "output_queues"): ("#1565C0", "produces",    False),
    # Compute → external (dashed)
    ("ecs",           "external"):      ("#546E7A", "HTTP",        True),
    ("lambdas",       "dynamodb"):      ("#2E7D32", "reads/writes",False),
    ("lambdas",       "storage"):       ("#2E7D32", "reads/writes",False),
    ("lambdas",       "external"):      ("#546E7A", "HTTP",        True),
    # DynamoDB streams → compute (purple)
    ("dynamodb",      "lambdas"):       ("#7B1FA2", "stream",      False),
    ("dynamodb",      "ecs"):           ("#7B1FA2", "stream",      False),
    # SNS fan-out → queues / lambdas (orange)
    ("sns",           "queues"):        ("#FF6D00", "fan-out",     False),
    ("sns",           "output_queues"): ("#FF6D00", "fan-out",     False),
    ("sns",           "lambdas"):       ("#FF6D00", "fan-out",     False),
}

# Edge types that use animated (dashed) arrows.
# Animated arrows look noisy when there are many edges.
_ANIMATED_EDGE_TYPES: frozenset[str] = frozenset()


class D2Generator:
    """
    Generates D2 diagram files from a ComponentGraph.
    Uses Jinja2 templates stored in engine/d2/templates/.

    Icons are resolved via get_resource_meta() so the taxonomy in models.py
    is the single source of truth — no more duplicate ICON_MAP here.
    """

    def __init__(self, templates_dir: Path):
        self.env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape([]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    # Template selection map: pattern → template filename (simplified/default)
    _PATTERN_TEMPLATES: dict[str, str] = {
        "ecs_microservice":    "L3_ecs_microservice.d2_template",
        "lambda_microservice": "L3_microservice.d2_template",
        "auto":                "L3_microservice.d2_template",  # overridden after auto-detect
        "generic":             "L3_microservice.d2_template",
    }

    # Template selection map: pattern → template filename (detailed/node-level)
    _PATTERN_TEMPLATES_DETAILED: dict[str, str] = {
        "ecs_microservice":    "L3_ecs_microservice_detailed.d2_template",
        "lambda_microservice": "L3_microservice.d2_template",
        "auto":                "L3_microservice.d2_template",
        "generic":             "L3_microservice.d2_template",
    }

    def generate_l3(
        self,
        component: Component,
        output_path: Path,
        edges: list | None = None,
        detail_level: str = "simplified",
        pattern: str = "auto",
    ) -> Path:
        """
        Generate an L3 (microservice detail) D2 diagram for a single component.

        The layout is determined by the ``pattern`` parameter:

        * ``ecs_microservice`` — uses L3_ecs_microservice.d2_template:
          ECS service in a central ``compute`` cluster, Lambda functions in a
          horizontal ``workers`` strip below, inputs/storage flanking left/right.
        * ``lambda_microservice`` / ``auto`` / anything else — uses the generic
          L3_microservice.d2_template (flat clusters, no hero separation).

        When ``pattern="auto"`` the template is selected by _detect_pattern().

        Args:
            component:   The resolved Component (clusters + nodes).
            output_path: Destination .d2 file path.
            edges:       Graph-level Edge list.  Only intra-component edges
                         will be rendered as arrows.
            pattern:     Layout pattern.  Passed down from the CLI ``--pattern``
                         flag or loaded from pndocgen.yaml.

        Returns:
            Path to the generated .d2 file.
        """
        # Resolve "auto" to the actual pattern by inspecting the component
        resolved_pattern = pattern
        if pattern == "auto":
            resolved_pattern = self._detect_pattern(component)

        # Select template based on pattern + detail_level
        if detail_level == "detailed":
            template_name = self._PATTERN_TEMPLATES_DETAILED.get(
                resolved_pattern, "L3_microservice.d2_template"
            )
        else:
            template_name = self._PATTERN_TEMPLATES.get(
                resolved_pattern, "L3_microservice.d2_template"
            )
        template = self.env.get_template(template_name)
        service_id = self._safe_id(component.name)

        # ── Build cluster node dicts ──────────────────────────────────────────
        clusters: dict[str, list[dict]] = {}
        # node_lookup: name → (cluster_name, safe_id) — used for edge resolution
        node_lookup: dict[str, tuple[str, str]] = {}

        for cluster_name, cluster in component.clusters.items():
            width, height = SIZE_MAP.get(cluster_name, (50, 50))
            node_dicts = []
            for node in cluster.nodes:
                meta = get_resource_meta(node.resource_type)
                resolved = _ICON_URL.get(meta.icon) if meta.icon else None
                icon = resolved or _FALLBACK_ICON
                safe = self._safe_id(node.name)
                # Strip component name prefix from label for readability.
                label = self._short_label(node.name, component.name)
                node_dicts.append({
                    "id":            safe,
                    "label":         label,
                    "icon":          icon,
                    "width":         width,
                    "height":        height,
                    "resource_type": node.resource_type,
                })
                node_lookup[node.name] = (cluster_name, safe)
            if node_dicts:
                clusters[cluster_name] = node_dicts

        # ── Post-process clusters based on pattern ───────────────────────────
        # For ecs_microservice (v74): tag_resolver already assigned nodes to the
        # correct named clusters (apigw, rules, queues, ecs, lambdas, dynamodb).
        # For legacy patterns: re-route lambdas from compute → workers.
        if resolved_pattern == "ecs_microservice" and "compute" in clusters:
            compute_nodes = clusters["compute"]
            new_compute = []
            workers = clusters.get("workers", [])
            for n in compute_nodes:
                if n["resource_type"] == "lambda":
                    workers.append(n)
                    for name, (old_c, safe_id) in node_lookup.items():
                        if old_c == "compute" and safe_id == n["id"]:
                            node_lookup[name] = ("workers", safe_id)
                else:
                    new_compute.append(n)
            if new_compute:
                clusters["compute"] = new_compute
            else:
                clusters.pop("compute", None)
            if workers:
                clusters["workers"] = workers

        # ── Producer / consumer queue split (ecs_microservice only) ──────────
        # Queues that are the TARGET of sqs_producer edges are "output queues"
        # (ECS/Lambda writes to them).  Move them from the "queues" cluster
        # (col2, left of ECS) to the new "output_queues" cluster (col4, right
        # of ECS alongside DynamoDB), so the flow reads naturally left→right:
        #   API GW → SQS(consumer) → ECS → SQS(producer)
        #                                 ↘ DynamoDB
        #
        # Only applies to simplified ecs_microservice layout; detailed (v65)
        # keeps all queues in col2 because node-level arrows already clarify direction.
        if resolved_pattern == "ecs_microservice" and detail_level == "simplified":
            producer_queue_names: set[str] = set()
            consumer_queue_names: set[str] = set()
            for edge in (edges or []):
                if edge.edge_type == "sqs_producer":
                    producer_queue_names.add(edge.target)
                elif edge.edge_type in ("sqs_consumer", "sqs_trigger"):
                    # sqs_consumer: source is the queue; sqs_trigger: source is the queue
                    consumer_queue_names.add(edge.source)

            # Only pure producers (not also consumers) get moved to output_queues.
            # Queues that are both consumed AND produced stay in col2 (queues) since
            # they participate in the bidirectional flow (e.g. safestore_to_raddalt).
            pure_producer_names = producer_queue_names - consumer_queue_names

            if pure_producer_names and clusters.get("queues"):
                remaining_queues = []
                output_queues = list(clusters.get("output_queues", []))
                for node_dict in clusters["queues"]:
                    # Find the original node name by reverse-looking through node_lookup
                    orig_name = next(
                        (n for n, (c, sid) in node_lookup.items()
                         if c == "queues" and sid == node_dict["id"]),
                        None,
                    )
                    if orig_name and orig_name in pure_producer_names:
                        output_queues.append(node_dict)
                        node_lookup[orig_name] = ("output_queues", node_dict["id"])
                        logger.debug(
                            f"[d2gen] Moved pure-producer queue {orig_name!r} → output_queues cluster"
                        )
                    else:
                        remaining_queues.append(node_dict)
                if remaining_queues:
                    clusters["queues"] = remaining_queues
                else:
                    clusters.pop("queues", None)
                if output_queues:
                    clusters["output_queues"] = output_queues

        # ── Build edges ───────────────────────────────────────────────────────
        if resolved_pattern == "ecs_microservice":
            if detail_level == "detailed":
                # v65 detailed: node-level edges with column path prefixes
                box_edges = []
                structural_edges = []
                data_edges = self._build_node_edges_v65(edges or [], node_lookup)
            else:
                # v74 simplified: box-level edges (cluster → cluster)
                box_edges = self._build_box_edges(edges or [], node_lookup)
                structural_edges = []
                data_edges = []
        else:
            box_edges = []
            structural_edges = self._build_structural_edges(resolved_pattern, clusters)
            data_edges = self._build_data_edges(
                edges or [], node_lookup, service_id, pattern=resolved_pattern
            )

        logger.debug(
            f"Pattern: {resolved_pattern}, detail_level: {detail_level}, "
            f"box_edges: {len(box_edges)}, structural: {len(structural_edges)}, "
            f"data: {len(data_edges)}"
        )

        context = {
            "service_name":      component.name,
            "service_id":        service_id,
            "clusters":          clusters,
            "alignment_edges":   self._build_alignment_edges(clusters),
            "structural_edges":  structural_edges,
            "data_edges":        data_edges,
            "box_edges":         box_edges,
        }

        d2_content = template.render(**context)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(d2_content)
        logger.info(
            f"Generated D2: {output_path} "
            f"(pattern={resolved_pattern}, detail={detail_level}, "
            f"{len(data_edges)} data edges, {len(structural_edges)} structural edges)"
        )
        return output_path

    def generate_l2(
        self,
        graph: ComponentGraph,
        output_path: Path,
        title: str = "",
    ) -> Path:
        """
        Generate an L2 (service map) D2 diagram showing all components and their
        cross-component dependencies.

        Each Component in the graph is rendered as a single labeled box.
        Only cross-component edges from graph.edges are shown (intra-component
        edges belong to L3 diagrams, not L2).

        Args:
            graph:       The full ComponentGraph (components + edges).
            output_path: Destination .d2 file path.
            title:       Diagram title shown in the header comment.

        Returns:
            Path to the generated .d2 file.
        """
        template = self.env.get_template("L2_service_map.d2_template")

        # Component names present in the graph (used for cross-edge filtering)
        component_names: set[str] = set(graph.components.keys())

        # ── Build component node dicts ────────────────────────────────────────
        comp_dicts = []
        for comp_name, component in graph.components.items():
            # Count total resources across all clusters
            resource_count = sum(
                len(cluster.nodes) for cluster in component.clusters.values()
            )
            # Pick a dominant icon: prefer ecs > lambda > first available
            dominant_icon = self._pick_dominant_icon(component)
            comp_dicts.append({
                "id":             self._safe_id(comp_name),
                "name":           comp_name,
                "resource_count": resource_count,
                "icon":           dominant_icon,
            })

        # ── Build cross-component edge dicts ─────────────────────────────────
        edge_dicts = []
        seen: set[tuple] = set()
        for edge in graph.edges:
            # Only render edges where BOTH source AND target are known components.
            # Intra-component edges (resource→resource within a component) are
            # ignored at L2 — they belong to L3 diagrams.
            if edge.source not in component_names or edge.target not in component_names:
                continue
            key = (edge.source, edge.target, edge.edge_type)
            if key in seen:
                continue
            seen.add(key)
            edge_dicts.append({
                "from_id":  self._safe_id(edge.source),
                "to_id":    self._safe_id(edge.target),
                "label":    edge.label or edge.edge_type,
                "edge_type": edge.edge_type,
                "animated": edge.edge_type in _ANIMATED_EDGE_TYPES,
            })

        context = {
            "title":      title,
            "components": comp_dicts,
            "edges":      edge_dicts,
        }

        d2_content = template.render(**context)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(d2_content)
        logger.info(
            f"Generated L2 D2: {output_path} "
            f"({len(comp_dicts)} components, {len(edge_dicts)} cross-component edges)"
        )
        return output_path

    def generate_all(
        self,
        graph: ComponentGraph,
        output_dir: Path,
        level: str = "all",
        pattern: str = "auto",
        detail_level: str = "simplified",
        ts: str | None = None,
        component_prefix: str | None = None,
    ) -> list[Path]:
        """
        Generate diagrams for all components in the graph.

        Args:
            graph:             The full ComponentGraph.
            output_dir:        Directory where .d2 files are written.
            level:             Which diagram levels to generate.
                               "l3"  → only per-component detail diagrams
                               "l2"  → only the service map (requires ≥2 components)
                               "all" → both L3 and L2 (default)
            pattern:           Layout pattern for L3 diagrams.
                               "auto" | "ecs_microservice" | "lambda_microservice"
            detail_level:      "simplified" | "detailed"
            ts:                Pre-computed timestamp string (YYYYMMDDHH).
                               When called from ``cmd_run()`` this is shared with the
                               inventory + graph files so all artifacts carry the same
                               timestamp.  If None, a fresh timestamp is generated.
            component_prefix:  Safe slug for the component (hyphens → underscores).
                               Used as the filename prefix when generating a single-
                               component run (e.g. "pn_delivery").  When None the
                               component name is used directly.

        File naming convention (new, Session 7):
            Single-component run (component_prefix provided):
                {output_dir}/{component_prefix}_{ts}_L3.d2
                {output_dir}/{component_prefix}_{ts}_L3_detailed.d2
                {output_dir}/{component_prefix}_{ts}_L2.d2   (service map)

            Multi-component run (component_prefix is None):
                {output_dir}/{safe_comp_name}_{ts}_L3.d2
                {output_dir}/service_map_{ts}_L2.d2

        Returns:
            List of generated .d2 file paths (L3 first, then L2 if applicable).
        """
        from datetime import datetime
        # Use the pre-computed timestamp from cmd_run() if available,
        # so all artifacts from one pipeline invocation share the same ts.
        if ts is None:
            ts = datetime.now().strftime("%Y%m%d%H%M")

        generated = []

        # ── L3: one diagram per component ────────────────────────────────────
        if level in ("l3", "all"):
            for name, component in graph.components.items():
                # Use the caller-supplied slug when available (single-component run),
                # otherwise derive it from the component name.
                file_prefix = component_prefix or self._safe_id(name)
                detail_suffix = f"_L3_detailed" if detail_level != "simplified" else "_L3"
                d2_path = output_dir / f"{file_prefix}_{ts}{detail_suffix}.d2"
                self.generate_l3(component, d2_path, edges=graph.edges, pattern=pattern, detail_level=detail_level)
                generated.append(d2_path)

        # ── L2: one service map for the whole graph ───────────────────────────
        if level in ("l2", "all") and len(graph.components) >= 2:
            map_prefix = component_prefix or "service_map"
            l2_path = output_dir / f"{map_prefix}_{ts}_L2.d2"
            self.generate_l2(graph, l2_path)
            generated.append(l2_path)
        elif level in ("l2", "all") and len(graph.components) < 2:
            logger.info("Skipping L2: need at least 2 components for a service map")

        logger.info("Generated %d D2 files in %s", len(generated), output_dir)
        return generated

    def render(self, d2_path: Path, fmt: str = "png") -> Path:
        """Render a ``.d2`` file to an image format via the D2 CLI.

        Supported formats:
        - ``png``
        - ``svg``
        """
        fmt_norm = (fmt or "png").strip().lower()
        if fmt_norm not in {"png", "svg"}:
            raise ValueError(f"Unsupported render format: {fmt}. Use 'png' or 'svg'.")

        output_path = d2_path.with_suffix(f".{fmt_norm}")
        cmd = ["d2", "--layout=elk", str(d2_path), str(output_path)]
        logger.info("Rendering (%s): %s", fmt_norm.upper(), " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error("d2 render failed: %s", result.stderr)
            raise RuntimeError(f"d2 render failed for {d2_path}: {result.stderr}")
        logger.info("Rendered %s: %s", fmt_norm.upper(), output_path)
        return output_path

    def render_png(self, d2_path: Path) -> Path:
        """Backward-compatible wrapper for PNG rendering."""
        return self.render(d2_path, fmt="png")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_id(name: str) -> str:
        """Convert resource name to a valid D2 identifier (no hyphens/dots)."""
        return name.replace("-", "_").replace(".", "_")

    @staticmethod
    def _short_label(node_name: str, component_name: str) -> str:
        """
        Return a short label for a node by stripping the component name prefix.

        Examples:
            "pn-delivery-insert-trigger-eb-lambda", "pn-delivery"
                → "insert-trigger-eb-lambda"
            "pn-delivery-versioning-v1v21-sendnewnotification-lambda", "pn-delivery"
                → "versioning-v1v21-send-newnotification-lambda"
            "pn-NotificationDelegationMetadata", "pn-delivery"
                → "NotificationDelegationMetadata"  (no match → strip "pn-" prefix only)

        Falls back to the full name if stripping would leave an empty string.
        """
        # Try stripping full component prefix + separator
        prefix = component_name + "-"
        if node_name.startswith(prefix):
            short = node_name[len(prefix):]
            return short if short else node_name

        # Try stripping project prefix for storage / shared resources
        _prefix = get_app_config().project.prefix
        if _prefix and node_name.startswith(_prefix):
            return node_name[len(_prefix):]

        return node_name

    @staticmethod
    def _pick_dominant_icon(component: "Component") -> str | None:
        """
        Return the icon path for the most representative resource type in a component.

        Priority: ecs_service > lambda > first node that has an icon.
        Used by generate_l2() to put a small icon inside each service box.
        """
        # Preferred types in priority order for L2 representation
        priority = ["ecs_service", "lambda", "step_function", "apigw", "apigw_v2"]
        all_nodes = [
            node
            for cluster in component.clusters.values()
            for node in cluster.nodes
        ]
        # Try priority order first
        for preferred_type in priority:
            for node in all_nodes:
                if node.resource_type == preferred_type:
                    meta = get_resource_meta(node.resource_type)
                    icon = _ICON_URL.get(meta.icon) if meta.icon else None
                    if icon:
                        return icon
        # Fallback: first node with any icon
        for node in all_nodes:
            meta = get_resource_meta(node.resource_type)
            icon = _ICON_URL.get(meta.icon) if meta.icon else None
            if icon:
                return icon
        return None

    @staticmethod
    def _build_alignment_edges(clusters: dict) -> list[tuple[str, str]]:
        """
        Build invisible alignment edges between cluster anchors to force
        left-to-right ordering: inputs → messaging → compute → storage.
        """
        order = ["inputs", "messaging", "compute", "storage"]
        present = [c for c in order if c in clusters and clusters[c]]
        edges = []
        for i in range(len(present) - 1):
            edges.append((present[i], present[i + 1]))
        return edges

    @staticmethod
    def _detect_pattern(component: "Component") -> str:
        """
        Detect the architectural pattern of a component.

        Patterns:
          "ecs_microservice" — has an ECS service as primary compute unit,
                               surrounded by SQS inputs, Lambda workers, and DynamoDB.
          "lambda_only"      — no ECS service, only Lambda functions.
          "generic"          — fallback for everything else.

        The pattern drives _build_structural_edges() which tells ELK
        how to place clusters relative to each other.
        """
        all_types = {
            node.resource_type
            for cluster in component.clusters.values()
            for node in cluster.nodes
        }
        has_ecs = "ecs_service" in all_types
        has_lambda = "lambda" in all_types
        has_storage = bool(component.clusters.get("storage"))
        has_inputs = bool(component.clusters.get("inputs")) or bool(component.clusters.get("messaging"))

        if has_ecs:
            return "ecs_microservice"
        if has_lambda and (has_storage or has_inputs):
            return "lambda_only"
        return "generic"

    @staticmethod
    def _build_structural_edges(pattern: str, clusters: dict) -> list[dict]:
        """
        Build invisible ordering edges for ELK.

        Returns:
            list[dict]: Items with {from_path, to_path}.
        """
        result: list[dict] = []

        # Per spostare la label verso l'inizio della freccia (lato source)
        # con D2/ELK, usiamo un edge-label "fantasma" su un anchor point vicino
        # alla sorgente. Questo evita di appoggiarsi alla piega ortogonale.
        #
        # Strategia:
        #   1) edge reale: mantiene stile/linea, ma senza label
        #   2) edge label-only: stessa direzione, opacità 0, con label.near
        #      impostato a outside-left-center (supportato da D2), così il testo
        #      resta più vicino all'origine del flusso.
        label_anchor_uid = 0

        if pattern != "ecs_microservice":
            # ── Generic / lambda_only: cluster-level ordering hints ───────────
            order = ["inputs", "messaging", "compute", "storage"]
            present = [c for c in order if c in clusters and clusters[c]]
            for i in range(len(present) - 1):
                result.append({"from_path": present[i], "to_path": present[i + 1]})
            return result

        # ecs_microservice: workers are merged into compute.
        # Use node-level invisible edges to stabilize layout ordering.

        compute_nodes = clusters.get("compute", [])
        if compute_nodes:
            ecs_node_dict = next(
                (n for n in compute_nodes if n.get("resource_type") == "ecs_service"),
                compute_nodes[0],
            )
            ecs_id = ecs_node_dict["id"]
            ecs_path = f"compute.{ecs_id}"

            # inputs/messaging node → ECS node (invisible)
            for cluster_name in ("inputs", "messaging"):
                for node in clusters.get(cluster_name, []):
                    result.append({
                        "from_path": f"{cluster_name}.{node['id']}",
                        "to_path":   ecs_path,
                    })

            # ECS node → storage (invisible)
            for node in clusters.get("storage", []):
                result.append({
                    "from_path": ecs_path,
                    "to_path":   f"storage.{node['id']}",
                })

            # ECS node → each worker lambda inside compute (invisible).
            # Workers are merged into compute cluster, so their path is compute.{id}
            for node in clusters.get("workers", []):
                result.append({
                    "from_path": ecs_path,
                    "to_path":   f"compute.{node['id']}",
                })

        return result

    @staticmethod
    def _node_path(service_id: str, cluster: str, node_id: str, pattern: str = "auto") -> str:
        """
        Return the D2 path for a node.

        For ecs_microservice, workers use the compute cluster path.
        Paths are compute.{node_id} for both compute and workers.

        For other patterns, paths are cluster_name.node_id.
        """
        if pattern == "ecs_microservice" and cluster == "workers":
            # workers are merged into compute in the template
            return f"compute.{node_id}"
        return f"{cluster}.{node_id}"

    @staticmethod
    def _build_data_edges(
        edges: list,
        node_lookup: dict[str, tuple[str, str]],
        service_id: str,
        hero: dict | None = None,
        pattern: str = "auto",
    ) -> list[dict]:
        """
        Build renderable edge dicts for data-flow arrows within a component.

        Only intra-component edges are rendered (both source AND target resolved
        to a node in this component's clusters). Cross-component edges belong to
        L2 diagrams, not L3.

        Hero nodes are stored in node_lookup with pseudo-cluster "_hero" and live
        directly inside service_id without a cluster wrapper.

        Returns a list of dicts with keys:
            from_path  – full D2 path for the source node
            to_path    – full D2 path for the target node
            label      – optional arrow label
            animated   – True for async/event-driven edge types
        """
        result = []
        seen = set()

        # Per ecs_microservice, trovare il path ECS per usarlo come relay
        ecs_path: str | None = None
        if pattern == "ecs_microservice":
            for node_name, (cluster, node_id) in node_lookup.items():
                if cluster == "compute":
                    ecs_path = D2Generator._node_path(service_id, cluster, node_id, pattern=pattern)
                    break

        for edge in edges:
            src = node_lookup.get(edge.source)
            tgt = node_lookup.get(edge.target)
            if not (src and tgt):
                continue
            key = (edge.source, edge.target, edge.edge_type)
            if key in seen:
                continue
            seen.add(key)

            src_path = D2Generator._node_path(service_id, src[0], src[1], pattern=pattern)
            tgt_path = D2Generator._node_path(service_id, tgt[0], tgt[1], pattern=pattern)

            # ecs_microservice routing rules:
            #  sqs_trigger  → queue triggers Lambda directly (EventSourceMapping)
            #                 Arrow: inputs.queue → service.workers.lambda  (direct)
            #  sqs_consumer → ECS polls queue (IAM policy); relay arrow via ECS
            #                 Arrow 1: inputs.queue → service.compute.ecs  (labeled)
            #                 Arrow 2: service.compute.ecs → service.workers.lambda (unlabeled)
            if (
                pattern == "ecs_microservice"
                and ecs_path
                and tgt[0] == "workers"
                and edge.edge_type == "sqs_consumer"
                and src_path != ecs_path
            ):
                # Relay: source → ECS → worker
                result.append({
                    "from_path": src_path,
                    "to_path":   ecs_path,
                    "label":     edge.label or "",
                    "animated":  False,
                })
                result.append({
                    "from_path": ecs_path,
                    "to_path":   tgt_path,
                    "label":     "",
                    "animated":  False,
                })
            else:
                # sqs_trigger and all others: direct arrow to target
                result.append({
                    "from_path": src_path,
                    "to_path":   tgt_path,
                    "label":     edge.label or "",
                    "animated":  edge.edge_type in _ANIMATED_EDGE_TYPES,
                })
        return result

    @staticmethod
    def _build_box_edges(
        edges: list,
        node_lookup: dict[str, tuple[str, str]],
    ) -> list[dict]:
        """
        Build box-level (cluster→cluster) edge dicts for the v74 ecs_microservice layout.

        Instead of routing arrows between individual nodes, aggregates all
        intra-component edges into cluster-pair relationships and emits one
        styled arrow per unique (src_cluster, dst_cluster) pair.

        The D2 path uses the v74 column structure:
            col1.apigw, col1.rules, col2.queues, col3.ecs,
            col3.lambdas, col4.dynamodb, col4.storage, col5.external

        Color coding mirrors the golden_v74 reference:
            blue   (#1565C0) — integrations / consume
            orange (#F57C00) — triggers (EventBridge, SQS→Lambda)
            green  (#2E7D32) — reads/writes to storage
            purple (#7B1FA2) — DDB streams back to compute
            grey   (#546E7A) — external HTTP calls (dashed)
        """
        edge_filter = get_app_config().edge_filter
        seen: set[tuple[str, str]] = set()
        result: list[dict] = []

        for edge in edges:
            # Apply edge type filter from pndocgen.yaml [edge_types] section
            if not edge_filter.is_allowed(edge.edge_type):
                continue

            src_info = node_lookup.get(edge.source)
            tgt_info = node_lookup.get(edge.target)
            if not (src_info and tgt_info):
                continue

            src_cluster = src_info[0]
            tgt_cluster = tgt_info[0]

            # Skip self-edges within the same cluster
            if src_cluster == tgt_cluster:
                continue

            key = (src_cluster, tgt_cluster)
            if key in seen:
                continue
            seen.add(key)

            # Resolve D2 paths using column structure
            src_box = _V74_BOX_PATH.get(src_cluster, src_cluster)
            tgt_box = _V74_BOX_PATH.get(tgt_cluster, tgt_cluster)

            # Lookup style from edge style map, fallback to neutral blue
            style = _V74_BOX_EDGE_STYLE.get(key)
            if style:
                stroke_color, label, stroke_dash = style
            else:
                stroke_color = "#1565C0"
                label = edge.label or edge.edge_type or ""
                stroke_dash = False

            result.append({
                "from_box":     src_box,
                "to_box":       tgt_box,
                "label":        label,
                "stroke_color": stroke_color,
                "stroke_dash":  stroke_dash,
                "animated":     False,
            })

        return result

    @staticmethod
    def _build_node_edges_v65(
        edges: list,
        node_lookup: dict[str, tuple[str, str]],
    ) -> list[dict]:
        """
        Build node-level edge dicts for the v65 detailed template.

        Unlike _build_box_edges (which aggregates to cluster level), this method
        emits one arrow per intra-component edge, with full D2 node paths:
            col2.queues.{node_id} → col3.ecs.{node_id}

        Color coding matches v65 golden reference:
            blue   (#1565C0) — SQS consumer / API GW integrations
            orange (#F57C00) — EventBridge / SQS triggers
            green  (#2E7D32) — storage reads/writes
            purple (#7B1FA2) — DDB streams
            grey   (#546E7A) — HTTP external (dashed)
        """
        # Maps (src_cluster, dst_cluster) → (stroke_color, stroke_dash)
        _STYLE_MAP: dict[tuple[str, str], tuple[str, bool]] = {
            ("apigw",    "lambdas"):  ("#1565C0", False),
            ("apigw",    "ecs"):      ("#1565C0", False),
            ("rules",    "lambdas"):  ("#F57C00", False),
            ("rules",    "ecs"):      ("#F57C00", False),
            ("rules",    "queues"):   ("#F57C00", False),
            ("queues",   "ecs"):      ("#1565C0", False),
            ("queues",   "lambdas"):  ("#F57C00", False),
            ("ecs",      "dynamodb"): ("#2E7D32", False),
            ("ecs",      "storage"):  ("#2E7D32", False),
            ("ecs",      "external"): ("#546E7A", True),
            ("ecs",      "queues"):   ("#1565C0", False),  # sqs_producer
            ("lambdas",  "dynamodb"): ("#2E7D32", False),
            ("lambdas",  "storage"):  ("#2E7D32", False),
            ("lambdas",  "external"): ("#546E7A", True),
            ("lambdas",  "queues"):   ("#1565C0", False),  # sqs_producer
            ("dynamodb", "lambdas"):  ("#7B1FA2", False),  # dynamodb_stream
            ("dynamodb", "ecs"):      ("#7B1FA2", False),  # dynamodb_stream
            ("sns",      "queues"):   ("#FF6D00", False),  # sns_subscription
            ("sns",      "lambdas"):  ("#FF6D00", False),  # sns_subscription
        }

        edge_filter = get_app_config().edge_filter
        seen: set[tuple] = set()
        result: list[dict] = []

        # Detect bidirectional SQS flow between same queue and compute node:
        # queue -> compute (sqs_consumer) + compute -> queue (sqs_producer).
        # Render as a single queue -> compute arrow labeled "consumes/produces".
        bidir_pairs: set[tuple[str, str]] = set()
        for edge in edges:
            if edge.edge_type != "sqs_consumer":
                continue
            if any(
                other.edge_type == "sqs_producer"
                and other.source == edge.target
                and other.target == edge.source
                for other in edges
            ):
                bidir_pairs.add((edge.source, edge.target))

        for edge in edges:
            # Apply edge type filter from pndocgen.yaml [edge_types] section
            if not edge_filter.is_allowed(edge.edge_type):
                continue

            # Collapse queue<->compute bidirectional SQS flow into one arrow.
            if edge.edge_type == "sqs_producer" and (edge.target, edge.source) in bidir_pairs:
                continue

            src_info = node_lookup.get(edge.source)
            tgt_info = node_lookup.get(edge.target)
            if not (src_info and tgt_info):
                continue

            src_cluster, src_id = src_info
            tgt_cluster, tgt_id = tgt_info

            key = (edge.source, edge.target, edge.edge_type)
            if key in seen:
                continue
            seen.add(key)

            # Build full D2 node paths using column structure
            src_col_prefix = _V74_BOX_PATH.get(src_cluster, src_cluster)
            tgt_col_prefix = _V74_BOX_PATH.get(tgt_cluster, tgt_cluster)
            from_path = f"{src_col_prefix}.{src_id}"
            to_path = f"{tgt_col_prefix}.{tgt_id}"

            style = _STYLE_MAP.get((src_cluster, tgt_cluster))
            stroke_color = style[0] if style else "#1565C0"
            stroke_dash = style[1] if style else False

            label = edge.label or ""
            if edge.edge_type == "sqs_consumer" and (edge.source, edge.target) in bidir_pairs:
                label = "consumes/produces"

            result.append({
                "from_path":    from_path,
                "to_path":      to_path,
                "label":        label,
                "stroke_color": stroke_color,
                "stroke_dash":  stroke_dash,
                "animated":     False,
            })

        return result
