"""Deterministic post-layout pipeline, with contracts emitted by the generator.

Legacy D2 files without metadata keep their existing 007 normalization only.
No inference of intended layouts from SVG geometry or regular expressions.
"""
import json
from pathlib import Path
import tempfile

from pndocgen.engine.core.normalization_config import NormalizationConfig
from pndocgen.engine.renderers.svg_alignment import Rule, normalize
from pndocgen.engine.renderers.svg_preferences import prefer_free_nodes, center_by_alternatives
from pndocgen.engine.renderers.svg_labels import separate_labels
from pndocgen.engine.renderers.svg_containers import fit_containers
from pndocgen.engine.renderers.svg_capacity import fit_ports
from pndocgen.engine.renderers.svg_node_labels import align_node_labels
from pndocgen.engine.renderers.svg_titles import separate_container_titles
from pndocgen.engine.renderers.svg_geometry import Diagram

CONTRACT_PREFIX = "# pndocgen-layout: "


def read_contracts(d2: Path):
    records = [line[len(CONTRACT_PREFIX):] for line in d2.read_text().splitlines()
               if line.startswith(CONTRACT_PREFIX)]
    if not records:
        return None
    if len(records) != 1:
        raise ValueError("Expected one pndocgen-layout record")
    data = json.loads(records[0])
    if (not isinstance(data, dict) or set(data) != {"version", "clusters"}
            or type(data["version"]) is not int or data["version"] != 1
            or not isinstance(data["clusters"], dict)):
        raise ValueError("Unsupported pndocgen-layout schema")
    for name, values in data["clusters"].items():
        if not name or not isinstance(values, dict) or set(values) != {"layout", "columns"}:
            raise ValueError("Invalid layout contract")
        Rule(**values)
    return data["clusters"]


def normalize_layout(source: Path, destination: Path, contracts,
                     settings: NormalizationConfig):
    if destination.exists() or destination.resolve() == source.resolve():
        raise FileExistsError(destination)
    report = {}
    current = source
    with tempfile.TemporaryDirectory(prefix=".svg-layout-", dir=source.parent) as folder:
        staging = Path(folder)
        if settings.enabled and contracts is not None:
            # Final fitting may undo temporary preparatory growth, but never
            # shrink below the original D2/normalizer container width.
            original_widths = {key: node.box[2] for key, node in
                               Diagram.load(source).containers.items()}
            rules = {name: Rule(**values, max_shift=settings.max_shift,
                               min_gap=settings.min_gap, min_terminal=settings.min_terminal)
                     for name, values in sorted(contracts.items())}
            # Establish safe text bounds before moving nodes. Otherwise a
            # recoverable initial overflow can block alignment, while fitting
            # only afterwards is too late for the measured-text gate.
            if settings.fit_text_containers:
                target = staging / "initial-containers.svg"
                report["initial_text_containers"] = fit_containers(
                    current, target, contracts, margin=settings.text_container_margin,
                    max_growth=settings.max_container_growth)
                current = target
            if settings.prefer_free_nodes:
                target = staging / "free.svg"
                report["free_nodes"] = prefer_free_nodes(current, target, rules)
                current = target
            target = staging / "aligned.svg"
            report["alignment"] = normalize(current, target, rules)
            current = target
            if settings.center_singletons or settings.center_grouped_nodes:
                target = staging / "centered.svg"
                report["centering"] = center_by_alternatives(
                    current, target, max_node_shift=settings.max_node_shift,
                    min_terminal=settings.min_terminal,
                    rules=rules if settings.center_grouped_nodes else None,
                    include_singletons=settings.center_singletons,
                    allowed_clusters={name for name, rule in rules.items() if rule.layout != "free"})
                current = target
            if settings.separate_edge_labels:
                target = staging / "labels.svg"
                report["labels"] = separate_labels(current, target, settings.max_label_shift)
                current = target
            if settings.port_capacity_classes:
                target = staging / 'capacity.svg'
                report['port_capacity'] = fit_ports(
                    current, target, allowed_classes=settings.port_capacity_classes,
                    max_growth=settings.max_port_icon_growth, max_shift=settings.max_node_shift,
                    padding=settings.port_icon_padding, contracts=contracts, min_gap=settings.min_gap,
                    min_terminal=settings.min_terminal)
                current = target
            # Correct ragged-grid label gaps before fitting the boundaries.
            if settings.align_node_labels:
                target = staging / 'node-labels.svg'
                report['node_labels'] = align_node_labels(current,target,gap=settings.node_label_gap)
                current = target
            if settings.fit_text_containers:
                target = staging / "containers.svg"
                report["text_containers"] = fit_containers(
                    current, target, contracts, margin=settings.text_container_margin,
                    max_growth=settings.max_container_growth, original_widths=original_widths)
                current = target
            if settings.separate_container_titles:
                target = staging / 'container-titles.svg'
                report['container_titles'] = separate_container_titles(
                    current, target, max_shift=settings.max_title_shift,
                    margin=settings.text_container_margin)
                current = target
        else:
            report["skipped"] = "disabled" if not settings.enabled else "no explicit layout metadata"
        with destination.open("xb") as handle:
            handle.write(current.read_bytes())
    return report
