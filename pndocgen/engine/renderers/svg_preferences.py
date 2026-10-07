"""Prefer free nodes and bounded singleton moves, derived from experiment 009."""
from __future__ import annotations

from pathlib import Path
import statistics
from pndocgen.engine.renderers import svg_alignment as base
from pndocgen.engine.renderers.svg_geometry import Diagram, EPS, collisions, find_port_spans, non_orthogonal_segments
from pndocgen.engine.renderers.svg_normalizer import (
    _serialize, _reload, _valid, _attachments, _label_distances,
    _shift_endpoint, _shift_terminal_segment,
)


def _gate(before, after, min_terminal=10.0):
    failure = _valid(after, _attachments(before), non_orthogonal_segments(before),
                     collisions(before), _label_distances(before))
    failure = failure or base._straight_direction_checks(before, after, min_terminal)
    from pndocgen.engine.renderers.svg_containers import measured_text_gate
    failure = failure or measured_text_gate(before, after)
    if not failure and base._stable_signature(before) != base._stable_signature(after):
        failure = "changed canvas, containers, node sizes, or topology"
    old = base._port_offsets(before)
    if not failure and any(value > old.get(key, value) + EPS
                           for key, value in base._port_offsets(after).items()):
        failure = "made an existing bundle less centered"
    return failure


def _save(source, destination, diagram, changed):
    if source.resolve() == destination.resolve():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as handle:
        handle.write(_serialize(diagram) if changed else source.read_bytes())


def prefer_free_nodes(source: Path, destination: Path, rules):
    """If attached siblings share one axis, move only their unconnected siblings.

    Applies to rows/columns with any number of children; conflicting attached
    nodes and grids are left to 008. The general alignment stage follows this.
    """
    if destination.exists():
        raise FileExistsError(destination)
    diagram = Diagram.load(source)
    report = {"applied": [], "skipped": []}
    if any(e.get("transform") for e in diagram.tree.getroot().iter()):
        report["skipped"].append({"cluster": "*", "reason": "unsupported transform"})
        _save(source, destination, diagram, False)
        return report
    for cluster, rule in sorted(rules.items()):
        if rule.layout not in {"row", "column"}:
            continue
        nodes = diagram.children_of(cluster)
        attached = [n for n in nodes if diagram.incident(n.node_id)]
        free = [n for n in nodes if not diagram.incident(n.node_id)]
        if not attached or not free:
            continue
        axis = 0 if rule.layout == "column" else 1
        coords = [n.center[axis] for n in attached]
        if max(coords) - min(coords) > EPS:
            continue
        anchor = statistics.median(coords)
        snapshot = _serialize(diagram)
        before = _reload(snapshot, source)
        moves = {}
        try:
            base.targets(diagram, cluster, rule)  # Geometry/nesting validation.
            for node in free:
                delta = anchor - node.center[axis]
                if abs(delta) > rule.max_shift + EPS:
                    raise ValueError("free-node move exceeds max_shift")
                if abs(delta) > EPS:
                    node.translate(delta if axis == 0 else 0, delta if axis == 1 else 0)
                    moves[node.node_id] = delta
            failure = _gate(before, diagram, rule.min_terminal)
        except ValueError as exc:
            failure = str(exc)
        if failure:
            diagram = before
            report["skipped"].append({"cluster": cluster, "reason": failure})
        elif moves:
            report["applied"].append({"cluster": cluster, "anchor": anchor, "moves": moves,
                                      "fixed_attached_nodes": [n.node_id for n in attached]})
    _save(source, destination, diagram, bool(report["applied"]))
    return report


def _side(node, edge, at_source):
    axis = edge.terminal_axis(at_source)
    point = edge.start if at_source else edge.end
    if axis == "x":
        return "destra" if point[0] >= node.center[0] else "sinistra"
    if axis == "y":
        return "sotto" if point[1] >= node.center[1] else "sopra"
    return None


def _candidate(before, node_id, axis, delta, min_terminal=10.0):
    """Move one node and independently center each incident bundle around it.

    A bundle whose offset equals the node movement stays completely fixed.
    An isolated port still inside the moved node's side also stays fixed.
    Other bundles move using the existing terminal/elbow transformation.
    """
    diagram = _reload(_serialize(before), before.path)
    node = diagram.nodes[node_id]
    old_node = before.nodes[node_id]
    spans = {s.side: s.offset for s in find_port_spans(before) if s.node_id == node_id}
    node.translate(delta if axis == "x" else 0, delta if axis == "y" else 0)
    transverse_sides = {"sinistra", "destra"} if axis == "y" else {"sopra", "sotto"}
    for edge, at_source in sorted(diagram.incident(node_id), key=lambda item: (item[0].edge_id, item[1])):
        if not edge.path.supported:
            return None, "unsupported incident path"
        side = _side(old_node, edge, at_source)
        if side is None:
            return None, "nonorthogonal incident terminal"
        if side in transverse_sides:
            point = edge.start if at_source else edge.end
            transverse = 1 if axis == "y" else 0
            # Preserve a singleton's path if its attachment remains on the
            # moved side. The shared gate verifies all endpoint attachments.
            if (side not in spans and
                    node.box[transverse] + EPS < point[transverse] < node.box[transverse] + node.box[transverse+2] - EPS):
                continue
            movement = delta - spans.get(side, 0.0)
            if abs(movement) > EPS:
                failure = _shift_terminal_segment(edge, at_source, movement, axis)
                if failure:
                    return None, failure
        elif abs(delta) > EPS:
            _shift_endpoint(edge, at_source, axis, delta)
    failure = _gate(before, diagram, min_terminal)
    if failure:
        return None, failure
    old = base._port_offsets(before)
    new = base._port_offsets(diagram)
    if not any(new.get(k, v) + EPS < v for k, v in old.items() if k[0] == node_id):
        return None, "no measurable centering improvement"
    return diagram, None


def center_by_alternatives(source: Path, destination: Path, *, max_node_shift=30.0,
                           min_terminal=10.0, allowed_clusters=None, rules=None,
                           include_singletons=True):
    """Finite local search: fixed node or node at each bundle's existing center.

    Singletons preserve the original behavior. Explicit row/column contracts
    also permit movement along their free axis, preserving sibling order/gaps.
    Grids and unspecified groups abstain. No cascade to other nodes/containers.
    """
    import math
    if not math.isfinite(max_node_shift) or max_node_shift < 0:
        raise ValueError("max_node_shift must be finite and nonnegative")
    if destination.exists():
        raise FileExistsError(destination)
    diagram = Diagram.load(source)
    report = {"applied": [], "skipped": [], "attempts": []}
    if any(e.get("transform") for e in diagram.tree.getroot().iter()):
        report["skipped"].append({"node": "*", "reason": "unsupported transform"})
        _save(source, destination, diagram, False)
        return report
    node_ids = sorted({s.node_id for s in find_port_spans(diagram) if abs(s.offset) > EPS})
    for node_id in node_ids:
        node = diagram.nodes[node_id]
        if allowed_clusters is not None and node.cluster_id not in allowed_clusters:
            report["skipped"].append({"node": node_id, "reason": "no explicit alignment contract"})
            continue
        siblings = diagram.children_of(node.cluster_id)
        grouped = len(siblings) != 1
        if not grouped and not include_singletons:
            continue
        rule = (rules or {}).get(node.cluster_id)
        if not node.cluster_id or (grouped and (rule is None or rule.layout not in {"column", "row"})):
            report["skipped"].append({"node": node_id, "reason": "non-singleton container"})
            continue
        if any(c.cluster_id == node.cluster_id for c in diagram.containers.values()):
            report["skipped"].append({"node": node_id, "reason": "nested container"})
            continue
        try:
            base.targets(diagram, node.cluster_id, base.Rule("free"))
        except ValueError as exc:
            report["skipped"].append({"node": node_id, "reason": str(exc)})
            continue
        if any(e.source == e.target for e, _ in diagram.incident(node_id)):
            report["skipped"].append({"node": node_id, "reason": "self-loop requires a separate contract"})
            continue
        old_paths = {e.edge_id: e.path.element.get("d") for e in diagram.edges}
        feasible = []
        for axis, sides in (("y", {"sinistra", "destra"}), ("x", {"sopra", "sotto"})):
            # A column permits movement along Y, never off its X alignment;
            # a row permits only X. Grids and unspecified groups still abstain.
            if grouped and axis != ("y" if rule.layout == "column" else "x"):
                continue
            offsets = [s.offset for s in find_port_spans(diagram)
                       if s.node_id == node_id and s.side in sides]
            if not any(abs(v) > EPS for v in offsets):
                continue
            for delta in sorted({0.0, *(round(v, 6) for v in offsets)}):
                if abs(delta) > max_node_shift + EPS:
                    report["attempts"].append({"node": node_id, "axis": axis, "delta": delta,
                                               "failure": "max_node_shift exceeded"})
                    continue
                candidate, failure = _candidate(diagram, node_id, axis, delta, min_terminal)
                if candidate is not None and grouped:
                    coordinate = 1 if axis == "y" else 0
                    old_order = sorted(siblings, key=lambda n: (n.center[coordinate], n.node_id))
                    new_order = sorted(candidate.children_of(node.cluster_id), key=lambda n: (n.center[coordinate], n.node_id))
                    if [n.node_id for n in old_order] != [n.node_id for n in new_order]:
                        failure = "sibling order changed"
                    for a, b in zip(old_order, old_order[1:]):
                        # Ragged columns can contain separate horizontal lanes.
                        # A vertical gap floor applies only to siblings whose
                        # transverse image spans overlap; the shared gate still
                        # checks every label, route and node in all lanes.
                        transverse = 1 - coordinate
                        if (a.box[transverse] + a.box[transverse+2] <= b.box[transverse] or
                                b.box[transverse] + b.box[transverse+2] <= a.box[transverse]):
                            continue
                        old_gap = b.box[coordinate] - a.box[coordinate] - a.box[coordinate+2]
                        na, nb = candidate.nodes[a.node_id], candidate.nodes[b.node_id]
                        new_gap = nb.box[coordinate] - na.box[coordinate] - na.box[coordinate+2]
                        if new_gap < min(old_gap, rule.min_gap) - EPS:
                            failure = "sibling gap below safety floor"
                    if failure:
                        candidate = None
                report["attempts"].append({"node": node_id, "axis": axis, "delta": delta,
                                           "failure": failure})
                if candidate is not None:
                    changed_paths = sum(e.path.element.get("d") != old_paths[e.edge_id]
                                        for e in candidate.edges)
                    # Stable preference: fewer changed edge paths, then smaller node move.
                    feasible.append(((changed_paths, abs(delta), axis, delta), candidate))
        if feasible:
            cost, diagram = min(feasible, key=lambda item: item[0])
            report["applied"].append({"node": node_id, "axis": cost[2], "delta": cost[3],
                                      "changed_paths": cost[0]})
        else:
            report["skipped"].append({"node": node_id, "reason": "no candidate passed every gate"})
    _save(source, destination, diagram, bool(report["applied"]))
    return report
