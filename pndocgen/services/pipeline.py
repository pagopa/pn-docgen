"""Service-layer orchestration functions used by the CLI entrypoints.

This module contains stateless functions that execute each pipeline stage
(``discover``, ``resolve``, ``generate``, ``run``) so the CLI module remains a
thin argument-parsing and dispatch layer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Any


def build_discovery_strategy(args: Any, session: Any = None, logger: Any = None) -> Any:
    """Build a discovery strategy from CLI arguments.

    Args:
        args: Parsed CLI namespace-like object.
        session: Optional boto3 session used by CFN-based strategies.
        logger: Optional logger for warnings.

    Returns:
        A concrete discovery strategy instance compatible with
        ``AWSDiscoverer(discovery_strategy=...)``.
    """
    from pndocgen.engine.discovery.tag_source import TagDiscoverySource
    from pndocgen.engine.discovery.cfn_source import CfnDiscoverySource, CompositeDiscoverySource
    from pndocgen.sources.aws.cfn_discoverer import CfnLiveDiscoverer

    source = getattr(args, "discovery_source", "tag") or "tag"
    component = getattr(args, "component", None)

    if source == "tag":
        return TagDiscoverySource(tag_key="Microservice")

    if source == "cfn":
        discoverer = CfnLiveDiscoverer(session, args.region)
        discoverer.discover(component_filter=component)
        return CfnDiscoverySource(discoverer)

    if source == "both":
        tag_strategy = TagDiscoverySource(tag_key="Microservice")
        discoverer = CfnLiveDiscoverer(session, args.region)
        discoverer.discover(component_filter=component)
        cfn_strategy = CfnDiscoverySource(discoverer)
        return CompositeDiscoverySource([tag_strategy, cfn_strategy])

    if logger:
        logger.warning("Unknown --discovery-source '%s', defaulting to 'tag'", source)
    return TagDiscoverySource(tag_key="Microservice")


def discover(args: Any, logger: Any = None) -> None:
    """Run the discovery phase and write the inventory JSON artifact.

    Supports both raw AWS API discovery and CFN-live discovery depending on the
    ``--discovery-source`` option.
    """
    import boto3
    from pndocgen.sources.aws.discoverer import AWSDiscoverer, _extract_account_env

    source = getattr(args, "discovery_source", "tag") or "tag"
    component = getattr(args, "component", None)
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account, env = _extract_account_env(args.profile)

    if source == "cfn":
        from pndocgen.sources.aws.cfn_discoverer import CfnLiveDiscoverer, DEFAULT_SKIP_TYPES

        raw_skip = getattr(args, "skip_types", None)
        if raw_skip is None:
            skip_cfn_types = DEFAULT_SKIP_TYPES
        elif raw_skip == "":
            skip_cfn_types = frozenset()
        else:
            skip_cfn_types = frozenset(t.strip() for t in raw_skip.split(",") if t.strip())

        cfn = CfnLiveDiscoverer(session, args.region)
        cfn.discover(component_filter=component)
        nodes = cfn.to_inventory_nodes(
            account=account,
            region=args.region,
            skip_cfn_types=skip_cfn_types,
        )
        relationships = cfn.to_edges()
        if logger:
            logger.info(
                "CFN-only mode: %d resources, %d relationships for component '%s' (skipping %d CFN types)",
                len(nodes),
                len(relationships),
                component,
                len(skip_cfn_types),
            )

    else:
        relationships = []
        strategy = build_discovery_strategy(args, session=session, logger=logger)
        discoverer = AWSDiscoverer(
            profile=args.profile,
            region=args.region,
            name_prefix=args.prefix,
            discovery_strategy=strategy,
        )
        nodes = discoverer.discover()
        account = discoverer.account
        env = discoverer.env

        if component:
            nodes = [
                n for n in nodes if n.metadata.get("component") == component or n.name.startswith(component)
            ]
            if logger:
                logger.info("Filtered to component '%s': %d resources", component, len(nodes))

    output = {
        "account": account,
        "env": env,
        "region": args.region,
        "discovery_source": source,
        "component_filter": component,
        "resources": [
            {
                "id": n.id,
                "name": n.name,
                "type": n.resource_type,
                "account": n.account,
                "region": n.region,
                "tags": n.tags,
                "metadata": n.metadata,
            }
            for n in nodes
        ],
        "relationships": relationships,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(f"Inventory saved to {out_path} ({len(nodes)} resources)")


def resolve(args: Any, logger: Any = None) -> None:
    """Run the resolve phase and write the component graph JSON artifact.

    This stage converts inventory resources into logical components/clusters,
    then enriches the graph with discovered relationships and optional static
    CFN repo analysis.
    """
    from pndocgen.engine.core.models import InventoryNode, Edge
    from pndocgen.engine.core.config import load_pattern
    from pndocgen.engine.resolvers.tag_resolver import TagResolver

    inventory = json.loads(Path(args.inventory).read_text())
    nodes = [
        InventoryNode(
            id=r["id"],
            name=r["name"],
            resource_type=r["type"],
            account=r["account"],
            region=r["region"],
            tags=r.get("tags", {}),
            metadata=r.get("metadata", {}),
        )
        for r in inventory["resources"]
    ]

    config_path = Path(args.config) if args.config else None
    cli_pattern = getattr(args, "pattern", None) or "auto"
    resolved_pattern = cli_pattern if cli_pattern != "auto" else load_pattern(config_path)
    include_dlq = bool(getattr(args, "include_dlq", False))
    include_external_queues = not bool(getattr(args, "exclude_external_queues", False))

    def _is_dlq_name(name: str) -> bool:
        n = (name or "").lower()
        return "dlq" in n or "dead-letter" in n or "dead_letter" in n

    def _inject_external_policy_queues(
        rels_data: list[dict],
        region: str,
    ) -> int:
        """Add synthetic SQS nodes for queues referenced by IAM edges but absent in component nodes."""
        if not include_external_queues or not graph.components:
            return 0

        queue_cluster = "queues" if resolved_pattern in ("auto", "ecs_microservice") else "messaging"
        existing_names_by_component: dict[str, set[str]] = {}
        for comp_name, comp in graph.components.items():
            names: set[str] = set()
            for cluster in comp.clusters.values():
                for node in cluster.nodes:
                    names.add(node.name)
            existing_names_by_component[comp_name] = names

        added = 0
        for rel in rels_data:
            edge_type = rel.get("type", "")
            if edge_type not in ("sqs_consumer", "sqs_producer"):
                continue

            if edge_type == "sqs_consumer":
                queue_name = rel.get("source", "")
                component_name = rel.get("target", "")
            else:
                queue_name = rel.get("target", "")
                component_name = rel.get("source", "")

            if not queue_name or component_name not in graph.components:
                continue
            if (not include_dlq) and _is_dlq_name(queue_name):
                continue

            existing_names = existing_names_by_component.setdefault(component_name, set())
            if queue_name in existing_names:
                continue

            comp = graph.components[component_name]
            synthetic_node = InventoryNode(
                id=f"external://sqs/{queue_name}",
                name=queue_name,
                resource_type="sqs",
                account=comp.account,
                region=region,
                metadata={
                    "component": component_name,
                    "external_reference": True,
                    "discovered_from": "iam_policy_edge",
                },
            )
            comp.add_node(queue_cluster, synthetic_node)
            existing_names.add(queue_name)
            added += 1

        return added

    resolver = TagResolver(
        config_path=config_path,
        pattern=resolved_pattern,
        include_dlq=include_dlq,
    )
    if logger:
        logger.info("Using pattern: %s", resolved_pattern)
        logger.info("Include DLQ: %s", include_dlq)
        logger.info("Include external policy queues: %s", include_external_queues)
    graph = resolver.resolve(nodes)

    rels = inventory.get("relationships", [])
    injected_queues = _inject_external_policy_queues(rels, region=inventory.get("region", ""))
    if injected_queues and logger:
        logger.info("Injected %d external SQS queue node(s) from IAM policy relationships", injected_queues)

    rel_edges = []
    for rel in rels:
        edge = Edge(
            source=rel["source"],
            target=rel["target"],
            edge_type=rel["type"],
            label=rel.get("label", ""),
        )
        graph.add_edge(edge)
        rel_edges.append(edge)
    if rels and logger:
        logger.info("Loaded %d relationships from inventory", len(rels))

    resolver.reroute_sqs_triggers(graph, rel_edges)

    if getattr(args, "repo_path", None):
        from pndocgen.sources.cfn.cfn_analyzer import CfnAnalyzer

        repo_path = Path(args.repo_path)
        if repo_path.exists():
            analyzer = CfnAnalyzer(repo_path=repo_path)
            analysis = analyzer.analyze()
            comp_name = analysis.name
            if comp_name in graph.components:
                graph.components[comp_name].analysis = analysis
            for edge in analysis.all_edges():
                graph.add_edge(edge)
            if logger:
                logger.info("Enriched graph with CFN analysis for %s", comp_name)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(graph.to_dict(), indent=2))
    print(f"Component graph saved to {out_path}")
    print(f"   Components: {len(graph.components)}")
    print(f"   Edges:      {len(graph.edges)}")
    print(f"   Orphans:    {len(graph.orphans)}")


def generate(args: Any, logger: Any = None) -> None:
    """Run the render phase and generate D2 (and optionally PNG/SVG) diagrams."""
    from pndocgen.engine.core.models import (
        InventoryNode,
        Component,
        ComponentGraph,
        Edge,
    )
    from pndocgen.engine.renderers.d2_generator import D2Generator

    graph_data = json.loads(Path(args.graph).read_text())
    graph = ComponentGraph()

    component_filter = getattr(args, "component", None)
    for comp_name, comp_data in graph_data["components"].items():
        if component_filter and not comp_name.startswith(component_filter):
            continue

        comp = Component(name=comp_name, account=comp_data.get("account", ""))
        for cluster_name, nodes_data in comp_data.get("clusters", {}).items():
            for n in nodes_data:
                node = InventoryNode(
                    id=n["id"],
                    name=n["name"],
                    resource_type=n["type"],
                    account=comp_data.get("account", ""),
                    region="",
                )
                comp.add_node(cluster_name, node)
        graph.add_component(comp)

    for e in graph_data.get("edges", []):
        graph.add_edge(
            Edge(
                source=e["source"],
                target=e["target"],
                edge_type=e["type"],
                label=e.get("label", ""),
            )
        )
    if graph.edges and logger:
        logger.info("Loaded %d edges from component graph", len(graph.edges))

    templates_dir = Path(__file__).parent.parent / "engine" / "d2" / "templates"
    generator = D2Generator(templates_dir=templates_dir)

    output_dir = Path(args.output_dir)
    level = getattr(args, "level", "all") or "all"
    gen_pattern = getattr(args, "pattern", "auto") or "auto"
    detail_level = getattr(args, "detail_level", "simplified") or "simplified"
    run_ts = getattr(args, "_run_ts", None)
    run_slug = getattr(args, "_run_slug", None)
    d2_files = generator.generate_all(
        graph,
        output_dir,
        level=level,
        pattern=gen_pattern,
        detail_level=detail_level,
        ts=run_ts,
        component_prefix=run_slug,
    )

    render_format = getattr(args, "render_format", "png") or "png"

    if args.render:
        for d2_file in d2_files:
            try:
                rendered = generator.render(d2_file, fmt=render_format)
                print(f"  {render_format.upper()}: {rendered}")
            except (RuntimeError, ValueError) as e:
                print(f"  WARNING: {e}")

    print(f"Generated {len(d2_files)} diagrams in {output_dir}")
    print(f"   Level:      {level}")
    print(f"   Components: {len(graph.components)}")
    print(f"   Edges:      {len(graph.edges)}")


def run(
    args,
    logger: Any = None,
    analyze_func: Callable[[Any], None] | None = None,
    discover_func: Callable[[Any], None] | None = None,
    resolve_func: Callable[[Any], None] | None = None,
    generate_func: Callable[[Any], None] | None = None,
) -> None:
    """Run the full end-to-end pipeline.

    The output layout is timestamped and grouped by component slug so each run
    produces a self-contained snapshot of inventory, graph, and diagrams.
    """
    from datetime import datetime

    if not discover_func or not resolve_func or not generate_func:
        raise ValueError("discover_func, resolve_func and generate_func are required")

    ts = datetime.now().strftime("%Y%m%d%H%M")
    component = getattr(args, "component", None)
    component_slug = component.replace("-", "_") if component else "all"

    output_dir_base = Path(args.output_dir) if args.output_dir else Path("docs/generated")
    output_dir = output_dir_base / component_slug
    output_dir.mkdir(parents=True, exist_ok=True)

    inventory_path = output_dir / f"{component_slug}_{ts}_inventory.json"
    graph_path = output_dir / f"{component_slug}_{ts}_graph.json"

    if getattr(args, "repo_path", None) and analyze_func:
        analysis_path = output_dir / f"{component_slug}_{ts}_analysis.json"
        args.output = str(analysis_path)
        analyze_func(args)

    args.output = str(inventory_path)
    discover_func(args)

    args.inventory = str(inventory_path)
    args.output = str(graph_path)
    resolve_func(args)

    args.graph = str(graph_path)
    args.output_dir = str(output_dir)
    args._run_ts = ts
    args._run_slug = component_slug
    generate_func(args)
