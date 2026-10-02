"""
pn-docgen CLI (v2)

Commands:
  pn-docgen discover  --profile <aws_profile> --region <region>
                      [--discovery-source tag|cfn|both]
                      [--component <name>] [--output inventory.json]
  pn-docgen resolve   --inventory inventory.json [--config pndocgen.yaml] [--include-dlq]
                      [--exclude-external-queues] [--output component_graph.json]
  pn-docgen generate  --graph component_graph.json [--component <name>] [--output-dir docs/generated/] [--render] [--render-format png|svg]
  pn-docgen analyze   --repo-path <path> [--output component_analysis.json]
  pn-docgen run       --profile <aws_profile> --region <region>
                      [--discovery-source tag|cfn|both]
                      [--component <name>] [--repo-path <path>]
                      [--output-dir docs/generated] [--include-dlq] [--exclude-external-queues]
                      [--render] [--render-format png|svg]

Output layout (run command):
  All generated files go to: {output-dir}/{component}/{component}_{YYYYMMDDHH}_*.ext
  Example: docs/generated/pn_delivery/pn_delivery_2026022219_inventory.json
           docs/generated/pn_delivery/pn_delivery_2026022219_graph.json
           docs/generated/pn_delivery/pn_delivery_2026022219_L3.d2
           docs/generated/pn_delivery/pn_delivery_2026022219_L3.png (or .svg)
"""

import json
import logging
import argparse
from pathlib import Path

from pndocgen.engine.core.config import VALID_RENDER_THEMES, get_app_config
from pndocgen.services.pipeline import (
    discover as discover_service,
    resolve as resolve_service,
    generate as generate_service,
    run as run_service,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("pndocgen")


def cmd_discover(args: argparse.Namespace) -> None:
    """Run the discovery stage via the service layer."""
    discover_service(args, logger=logger)



# ---------------------------------------------------------------------------
# analyze (CFN static analysis)
# ---------------------------------------------------------------------------

def cmd_analyze(args: argparse.Namespace) -> None:
    """Run static repository analysis and write ``component_analysis.json``.

    Args:
        args: Parsed CLI arguments namespace.
    """
    from pndocgen.sources.cfn.cfn_analyzer import CfnAnalyzer

    repo_path = Path(args.repo_path)
    if not repo_path.exists():
        print(f"ERROR: Repo path not found: {repo_path}")
        return

    analyzer = CfnAnalyzer(repo_path=repo_path)
    analysis = analyzer.analyze()

    result = analysis.to_dict()

    # Pretty print to console
    print(f"\nComponent: {analysis.name}")
    print(f"\nOwned SQS queues ({len(analysis.owned_queues)}):")
    for q in analysis.owned_queues:
        print(f"  {q}")
    print(f"\nProducer queues ({len(analysis.producer_queues)}):")
    for q in analysis.producer_queues:
        print(f"  -> {q}")
    print(f"\nConsumer queues ({len(analysis.consumer_queues)}):")
    for q in analysis.consumer_queues:
        print(f"  <- {q}")
    print(f"\nHTTP clients ({len(analysis.http_clients)}):")
    for svc in analysis.http_clients:
        print(f"  -> {svc}")
    print(f"\nLambdas ({len(analysis.lambdas)}):")
    for fn in analysis.lambdas:
        print(f"  {fn}")
    print(f"\nInternal deps from pom.xml ({len(analysis.internal_deps)}):")
    for dep in analysis.internal_deps:
        print(f"  {dep}")

    # Save JSON output
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2))
    print(f"\nAnalysis saved to {out_path}")


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

def cmd_resolve(args: argparse.Namespace) -> None:
    """Run the resolve stage via the service layer."""
    resolve_service(args, logger=logger)


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

def cmd_generate(args: argparse.Namespace) -> None:
    """Run the generate stage via the service layer."""
    generate_service(args, logger=logger)


# ---------------------------------------------------------------------------
# run (full pipeline)
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> None:
    """Full pipeline: [analyze] → discover → resolve → generate.

    All output files are written to:
        {output_dir}/{component_slug}/{component_slug}_{ts}_*.ext

    where ts = YYYYMMDDHH (year+month+day+hour, no minutes/seconds).
    Intermediate files (inventory, graph) are kept alongside the diagrams
    so each run produces a complete, self-contained snapshot.
    """
    run_service(
        args,
        logger=logger,
        analyze_func=cmd_analyze,
        discover_func=cmd_discover,
        resolve_func=cmd_resolve,
        generate_func=cmd_generate,
    )


# ---------------------------------------------------------------------------
# CLI definition
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse command-line arguments and dispatch to the selected command."""
    parser = argparse.ArgumentParser(
        prog="pn-docgen",
        description=f"pn-docgen: {get_app_config().project.description or 'AWS Architecture Diagram Generator'}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- analyze ---
    p_ana = subparsers.add_parser("analyze", help="Static analysis of a microservice repo (CFN + pom.xml)")
    p_ana.add_argument("--repo-path", required=True, help="Path to the microservice repo (e.g. repo_link/pn-delivery)")
    p_ana.add_argument("--output", default="component_analysis.json")
    p_ana.set_defaults(func=cmd_analyze)

    # --- discover ---
    p_disc = subparsers.add_parser("discover", help="Discover AWS resources")
    p_disc.add_argument("--config", default=None, help="Path to central pndocgen.yaml")
    p_disc.add_argument("--profile", required=True, help="AWS profile (e.g. sso_pn-core-dev)")
    p_disc.add_argument("--region", required=True, help="AWS region (e.g. eu-south-1)")
    p_disc.add_argument("--prefix", default=None, help="Resource name prefix filter (default: from pndocgen.yaml project.prefix)")
    p_disc.add_argument("--component", default=None, help="Filter to a single component (e.g. pn-delivery)")
    p_disc.add_argument(
        "--discovery-source",
        dest="discovery_source",
        choices=["tag", "cfn", "both"],
        default="tag",
        help="How to assign resources to components: tag (Microservice tag), cfn (CloudFormation stacks), both"
    )
    p_disc.add_argument(
        "--skip-types",
        dest="skip_types",
        default=None,
        help=(
            "Comma-separated CFN resource types to exclude from inventory "
            "(default: alarms, log groups, IAM, etc.). "
            "Pass empty string to include everything. "
            "Example: --skip-types 'AWS::CloudWatch::Alarm,AWS::IAM::Role'"
        )
    )
    p_disc.add_argument("--output", default="inventory.json",
                        help="Output path for inventory JSON (default: inventory.json)")
    p_disc.set_defaults(func=cmd_discover)


    _pattern_help = (
        "Diagram layout pattern for L3 diagrams. "
        "auto = detect automatically (ecs_microservice if ECS service is present), "
        "ecs_microservice = ECS service as central node + Lambda workers strip below, "
        "lambda_microservice = Lambda functions as primary compute grid. "
        "Can also be set in pndocgen.yaml (pattern: ecs_microservice)."
    )
    _theme_help = (
        "Diagram colour theme. white = transparent cluster fills on a white canvas; "
        "pastel = colour-coded cluster fills. Overrides render.theme in pndocgen.yaml."
    )

    # --- resolve ---
    p_res = subparsers.add_parser("resolve", help="Resolve inventory into component graph")
    p_res.add_argument("--inventory", default="inventory.json")
    p_res.add_argument("--config", default=None, help="Path to pndocgen.yaml")
    p_res.add_argument("--repo-path", default=None, help="Repo path for CFN enrichment")
    p_res.add_argument("--output", default="component_graph.json",
                       help="Output path for component graph JSON (default: component_graph.json)")
    p_res.add_argument(
        "--pattern",
        choices=["auto", "ecs_microservice", "lambda_microservice"],
        default="auto",
        help=_pattern_help,
    )
    p_res.add_argument(
        "--include-dlq",
        action="store_true",
        help="Include DLQ queues in the resolved graph (default: excluded)",
    )
    p_res.add_argument(
        "--exclude-external-queues",
        action="store_true",
        help=(
            "Exclude SQS queues referenced by IAM policies but not discovered in "
            "component stacks (default: included)"
        ),
    )
    p_res.set_defaults(func=cmd_resolve)

    # --- generate ---
    p_gen = subparsers.add_parser("generate", help="Generate D2 diagrams")
    p_gen.add_argument("--graph", default="component_graph.json")
    p_gen.add_argument("--config", default=None, help="Path to pndocgen.yaml")
    p_gen.add_argument("--component", default=None, help="Generate only for this component")
    p_gen.add_argument("--ingress-layout", choices=["peers", "legacy"], default=None,
                       help="Override central ingress layout policy for ECS detailed views")
    p_gen.add_argument("--output-dir", default="docs/generated/",
                       help="Base output directory for generated diagrams (default: docs/generated/)")
    p_gen.add_argument(
        "--level",
        choices=["l2", "l3", "all"],
        default="all",
        help=(
            "Which diagram levels to generate. "
            "l3 = per-component detail (default cluster layout), "
            "l2 = service map (cross-component dependencies, requires ≥2 components), "
            "all = both l3 and l2 (default)"
        ),
    )
    p_gen.add_argument(
        "--pattern",
        choices=["auto", "ecs_microservice", "lambda_microservice"],
        default="auto",
        help=_pattern_help,
    )
    p_gen.add_argument(
        "--detail-level",
        dest="detail_level",
        choices=["simplified", "detailed"],
        default="simplified",
        help="L3 edge detail level: simplified = box-level edges (default), detailed = node-level edges",
    )
    p_gen.add_argument(
        "--theme",
        choices=sorted(VALID_RENDER_THEMES),
        default=None,
        help=_theme_help,
    )
    p_gen.add_argument("--render", action="store_true", help="Also render an image via d2 CLI")
    p_gen.add_argument(
        "--render-format",
        choices=["png", "svg"],
        default="svg",
        help="Render output format when --render is enabled (default: svg; PNG skips normalization)",
    )
    p_gen.set_defaults(func=cmd_generate)

    # --- run (full pipeline) ---
    p_run = subparsers.add_parser("run", help="Full pipeline: analyze → discover → resolve → generate")
    p_run.add_argument("--profile", required=True)
    p_run.add_argument("--region", required=True)
    p_run.add_argument("--prefix", default=None, help="Resource name prefix filter (default: from pndocgen.yaml project.prefix)")
    p_run.add_argument("--component", default=None, help="Focus on a single component")
    p_run.add_argument("--repo-path", default=None, help="Repo path for CFN static analysis")
    p_run.add_argument(
        "--discovery-source",
        dest="discovery_source",
        choices=["tag", "cfn", "both"],
        default="tag",
        help="How to assign resources to components: tag (Microservice tag), cfn (CloudFormation stacks), both"
    )
    p_run.add_argument("--config", default=None, help="Path to pndocgen.yaml")
    p_run.add_argument(
        "--include-dlq",
        action="store_true",
        help="Include DLQ queues in the resolved graph (default: excluded)",
    )
    p_run.add_argument(
        "--exclude-external-queues",
        action="store_true",
        help=(
            "Exclude SQS queues referenced by IAM policies but not discovered in "
            "component stacks (default: included)"
        ),
    )
    p_run.add_argument(
        "--output-dir", default="docs/generated",
        help=(
            "Base output directory. Files are written to {output-dir}/{component}/. "
            "Default: docs/generated"
        ),
    )
    p_run.add_argument("--render", action="store_true")
    p_run.add_argument(
        "--render-format",
        choices=["png", "svg"],
        default="svg",
        help="Render output format when --render is enabled (default: svg; PNG skips normalization)",
    )
    p_run.add_argument(
        "--pattern",
        choices=["auto", "ecs_microservice", "lambda_microservice"],
        default="auto",
        help=_pattern_help,
    )
    p_run.add_argument(
        "--detail-level",
        dest="detail_level",
        choices=["simplified", "detailed"],
        default="simplified",
        help="L3 edge detail level: simplified = box-level edges (default), detailed = node-level edges",
    )
    p_run.add_argument(
        "--theme",
        choices=sorted(VALID_RENDER_THEMES),
        default=None,
        help=_theme_help,
    )
    p_run.add_argument(
        "--level",
        choices=["l2", "l3", "all"],
        default="all",
        help="Which diagram levels to generate (l3, l2, or all)",
    )
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
