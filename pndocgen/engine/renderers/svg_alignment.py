"""Explicit, guarded row/column/grid alignment derived from experiment 008."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, asdict
from pathlib import Path

from pndocgen.engine.renderers.svg_geometry import (
    Diagram, EPS, NS, decode_id, collisions, non_orthogonal_segments, find_port_spans,
)
from pndocgen.engine.renderers.svg_normalizer import (
    _serialize, _reload, _attachments, _label_distances, _valid,
    _shift_endpoint, _shift_terminal_segment,
)


@dataclass(frozen=True)
class Rule:
    layout: str  # column, row, grid, free
    columns: int = 1
    max_shift: float = 120.0
    min_gap: float = 24.0
    min_terminal: float = 10.0

    def __post_init__(self):
        if self.layout not in {"column", "row", "grid", "free"}:
            raise ValueError(f"Unknown layout: {self.layout}")
        if type(self.columns) is not int or self.columns < 1:
            raise ValueError("columns must be a positive integer")
        if any(not math.isfinite(v) or v < 0 for v in
               (self.max_shift, self.min_gap, self.min_terminal)):
            raise ValueError("Thresholds must be finite and nonnegative")


def _grid_columns(nodes, count):
    """Partition visibly separate columns, refusing ambiguous membership.

    Column count is explicit. Node membership comes from disjoint horizontal
    bounds. Overlapping bands are not split by arbitrary ordering or node name.
    """
    bands = []
    right = None
    for node in sorted(nodes, key=lambda n: (n.box[0], n.center[1], n.node_id)):
        x, _, width, _ = node.box
        if right is None or x >= right - EPS:
            bands.append([])
            right = x + width
        else:
            right = max(right, x + width)
        bands[-1].append(node)
    if len(bands) != count:
        raise ValueError(f"grid membership ambiguous: expected {count} columns, found {len(bands)}")
    return [sorted(band, key=lambda n: (n.center[1], n.node_id)) for band in bands]


def targets(diagram: Diagram, cluster: str, rule: Rule):
    """Compute a complete target before any movement. Same algorithm for all shapes."""
    edge_ids = {edge.edge_id for edge in diagram.edges}
    for group in diagram.tree.getroot().iter(NS + "g"):
        identifier = decode_id(group.get("class", ""))
        if (identifier and not identifier.startswith("(") and "." in identifier
                and identifier.rsplit(".", 1)[0] == cluster
                and identifier not in diagram.nodes and identifier not in diagram.containers
                and identifier not in edge_ids):
            raise ValueError(f"unsupported child geometry: {identifier}")
    nodes = diagram.children_of(cluster)
    planned = {n.node_id: n.center for n in nodes}
    if rule.layout == "free" or len(nodes) < 2:
        return planned
    if any(c.cluster_id == cluster for c in diagram.containers.values()):
        raise ValueError("mixed nested containers: explicit leaf contracts required")
    if rule.layout in {"column", "row"}:
        axis = 0 if rule.layout == "column" else 1
        anchor = statistics.median(n.center[axis] for n in nodes)
        for node in nodes:
            point = list(node.center)
            point[axis] = anchor
            planned[node.node_id] = tuple(point)
    else:
        columns = _grid_columns(nodes, rule.columns)
        rows = max(map(len, columns))
        ys = [n.center[1] for n in nodes]
        low, high = min(ys), max(ys)
        # Equal row spacing in the existing occupied height. Ragged columns
        # retain their row order and align by row index, including 4/3/3 grids.
        pitch = (high - low) / (rows - 1) if rows > 1 else 0.0
        if rows > 1 and pitch < max(n.box[3] for n in nodes) + rule.min_gap:
            raise ValueError("insufficient height for grid rows and required gap")
        for column in columns:
            x = statistics.median(n.center[0] for n in column)
            for row, node in enumerate(column):
                planned[node.node_id] = (x, low + row * pitch)
    for node in nodes:
        dest = planned[node.node_id]
        if max(abs(dest[i] - node.center[i]) for i in (0, 1)) > rule.max_shift + EPS:
            raise ValueError("requested move exceeds max_shift")
    return planned


def _stable_signature(diagram):
    return {
        "canvas": diagram.viewbox,
        "containers": {key: n.box for key, n in diagram.containers.items()},
        "nodes": {key: n.box[2:] for key, n in diagram.nodes.items()},
        "edges": {e.edge_id: (e.source, e.target, tuple(c.letter for c in e.path.commands))
                  for e in diagram.edges},
    }


def _port_offsets(diagram):
    return {(s.node_id, s.side): abs(s.offset) for s in find_port_spans(diagram)}


def _straight_direction_checks(before, after, minimum):
    """Guard against shortened/inverted straight sections, not just diagonal lines."""
    old_edges = {e.edge_id: e for e in before.edges}
    for edge in after.edges:
        old = old_edges[edge.edge_id]
        for i, (a, b) in enumerate(zip(old.path.commands, edge.path.commands)):
            if i == 0 or a.letter != "L":
                continue
            old_start = old.path.commands[i-1].anchor
            new_start = edge.path.commands[i-1].anchor
            if a.values == b.values and old_start == new_start:
                continue
            old_vec = [a.anchor[j] - old_start[j] for j in (0, 1)]
            new_vec = [b.anchor[j] - new_start[j] for j in (0, 1)]
            axis = 0 if abs(old_vec[0]) > abs(old_vec[1]) else 1
            if old_vec[axis] * new_vec[axis] <= 0:
                return "straight segment reversed or collapsed"
            if abs(new_vec[axis]) < minimum - EPS:
                return "straight segment shorter than min_terminal"
    return None


def _move_edges(diagram, displacements):
    for edge in sorted(diagram.edges, key=lambda e: e.edge_id):
        for at_source in (True, False):
            node_id = edge.source if at_source else edge.target
            dx, dy = displacements.get(node_id, (0.0, 0.0))
            if abs(dx) <= EPS and abs(dy) <= EPS:
                continue
            if not edge.path.supported:
                return "incident path unsupported"
            axis = edge.terminal_axis(at_source)
            if axis is None:
                return "incident terminal is not orthogonal"
            # Transverse movement carries the elbow and its labels/masks.
            perpendicular = dy if axis == "x" else dx
            parallel = dx if axis == "x" else dy
            if abs(perpendicular) > EPS:
                refusal = _shift_terminal_segment(edge, at_source, perpendicular,
                                                  "y" if axis == "x" else "x")
                if refusal:
                    return refusal
            if abs(parallel) > EPS:
                _shift_endpoint(edge, at_source, axis, parallel)
    return None


def normalize(source: Path, destination: Path, rules: dict[str, Rule]):
    """Apply each cluster atomically; preserve original SVG on abstention.

    Deliberately refuses existing destinations, including the source itself.
    No special IDs, AWS resource categories, or service names in the algorithm.
    """
    if destination.exists() or destination.resolve() == source.resolve():
        raise FileExistsError(destination)
    diagram = Diagram.load(source)
    report = {"applied": [], "unchanged": [], "skipped": [], "rules": {
        name: asdict(rule) for name, rule in sorted(rules.items())}}
    if any(e.get("transform") for e in diagram.tree.getroot().iter()):
        report["skipped"].append({"cluster": "*", "reason": "unsupported coordinate transform"})
        payload = source.read_bytes()
    else:
        modified = False
        for cluster in sorted(set(diagram.containers) | set(rules)):
            rule = rules.get(cluster)
            if rule is None or rule.layout == "free":
                report["skipped"].append({"cluster": cluster, "reason": "no explicit alignment contract"})
                continue
            if cluster not in diagram.containers:
                report["skipped"].append({"cluster": cluster, "reason": "container not found"})
                continue
            snapshot = _serialize(diagram)
            before = _reload(snapshot, source)
            try:
                plan = targets(diagram, cluster, rule)
            except ValueError as exc:
                report["skipped"].append({"cluster": cluster, "reason": str(exc)})
                continue
            moves = {key: tuple(plan[key][i] - diagram.nodes[key].center[i] for i in (0, 1))
                     for key in plan}
            moves = {key: d for key, d in moves.items() if max(map(abs, d)) > EPS}
            if not moves:
                report["unchanged"].append(cluster)
                continue
            # Move all nodes before testing, so pairs/whole rows are evaluated together.
            for key, (dx, dy) in moves.items():
                diagram.nodes[key].translate(dx, dy)
            failure = _move_edges(diagram, moves)
            failure = failure or _valid(diagram, _attachments(before), non_orthogonal_segments(before),
                                        collisions(before), _label_distances(before))
            failure = failure or _straight_direction_checks(before, diagram, rule.min_terminal)
            from pndocgen.engine.renderers.svg_containers import measured_text_gate
            failure = failure or measured_text_gate(before, diagram)
            if not failure and _stable_signature(before) != _stable_signature(diagram):
                failure = "canvas, containers, sizes, or graph topology changed"
            if not failure:
                old_ports = _port_offsets(before)
                if any(v > old_ports.get(k, v) + EPS for k, v in _port_offsets(diagram).items()):
                    failure = "port bundle became less centered"
            if failure:
                diagram = before
                report["skipped"].append({"cluster": cluster, "reason": failure})
            else:
                modified = True
                report["applied"].append({"cluster": cluster, "layout": rule.layout,
                                          "moves": moves})
        payload = _serialize(diagram) if modified else source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as handle:
        handle.write(payload)
    return report
