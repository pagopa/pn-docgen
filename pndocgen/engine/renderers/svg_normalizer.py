"""Passaggio di simmetrizzazione dell'SVG: allineamento dei nodi e centraggio dei fasci.

Derivato dall'esperimento 007 congelato. Correzioni locali applicate a un layout
gia' calcolato, senza aggiungere pieghe o cambiare topologia: sposta un
nodo sull'asse di maggioranza del proprio cluster e si limita ad allungare o
accorciare i segmenti terminali degli archi incidenti. Se una condizione non e'
verificabile il passaggio **si astiene** e lascia il cluster com'era.
"""

from __future__ import annotations

import io
import math
import os
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from pndocgen.engine.renderers.svg_geometry import (
    CORNER,
    EPS,
    Diagram,
    Edge,
    find_outliers,
    find_port_spans,
    image_overlaps,
    non_orthogonal_segments,
    outside_container,
    outside_viewbox,
    collisions,
    label_segment,
)


@dataclass
class Report:
    resized: int = 0
    aligned: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    centered: list[str] = field(default_factory=list)
    rolled_back: list[str] = field(default_factory=list)


def _serialize(diagram: Diagram) -> bytes:
    buffer = io.BytesIO()
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    diagram.tree.write(buffer, encoding="utf-8", xml_declaration=True)
    return buffer.getvalue()


def _reload(data: bytes, path: Path) -> Diagram:
    return Diagram.from_tree(ET.ElementTree(ET.fromstring(data)), path)


def _point_to_box(point: tuple[float, float], box: tuple[float, float, float, float]) -> float:
    px, py = point
    x, y, w, h = box
    dx = max(x - px, 0.0, px - (x + w))
    dy = max(y - py, 0.0, py - (y + h))
    return (dx * dx + dy * dy) ** 0.5


def _attachments(diagram: Diagram) -> dict[tuple[str, bool], float]:
    distances: dict[tuple[str, bool], float] = {}
    for edge in diagram.edges:
        if not edge.path.supported:
            continue
        for at_source in (True, False):
            node = diagram.nodes.get(edge.source if at_source else edge.target)
            if node is None:
                continue
            point = edge.start if at_source else edge.end
            distances[(edge.edge_id, at_source)] = _point_to_box(point, node.box)
    return distances


def _shift_endpoint(edge: Edge, at_source: bool, axis: str, delta: float) -> None:
    """Allunga o accorcia il segmento terminale spostandone il solo estremo."""
    command = edge.path.commands[0 if at_source else -1]
    index = -2 if axis == "x" else -1
    command.values[index] += delta
    edge.path.write()


def _shift_terminal_segment(edge: Edge, at_source: bool, delta: float, axis: str) -> str | None:
    """Trasla in blocco il segmento terminale e l'angolo che lo raccorda.

    Il segmento ortogonale successivo si accorcia o si allunga: nessuna piega
    viene aggiunta e il numero di punti del percorso resta invariato.
    """
    commands = edge.path.commands
    if not edge.path.supported:
        return "percorso non supportato"
    if len(commands) < 4:
        return "percorso senza pieghe: si sposterebbe anche l'altro estremo"

    candidates = [i for i, c in enumerate(commands) if c.letter == "S"]
    if not candidates:
        return "raccordo terminale non riconosciuto"
    corner_index = candidates[0] if at_source else candidates[-1]
    if corner_index < 2 or corner_index + 1 >= len(commands):
        return "raccordo terminale non riconosciuto"
    # A D2 C-shaped jog before the first S elbow travels rigidly with the
    # terminal section. All cubic control points are retained; no rerouting.
    touched = commands[:corner_index + 1] if at_source else commands[corner_index - 1:]
    moving = commands[corner_index] if at_source else commands[corner_index - 1]
    fixed = commands[corner_index + 1] if at_source else commands[corner_index - 2]
    index = 0 if axis == "x" else 1
    if abs(moving.anchor[1-index] - fixed.anchor[1-index]) > EPS:
        return "segmento adiacente non parallelo allo spostamento"
    old_length = fixed.anchor[index] - moving.anchor[index]
    new_length = old_length - delta
    if old_length * new_length <= 0 or abs(new_length) < CORNER:
        return "segmento adiacente troppo corto dopo lo spostamento"

    label_anchor = label_segment(edge)
    if edge.label is not None and label_anchor is None:
        return "label senza maschera geometrica riconosciuta"
    move_label = label_anchor is not None and (
        label_anchor[0] < corner_index if at_source else label_anchor[0] > corner_index
    )
    if move_label:
        # The label and the hole in D2's shared path mask travel together.
        # Otherwise an aligned bundle can leave a floating label and a stale gap.
        attribute = "x" if axis == "x" else "y"
        for element in (edge.label, edge.label_mask):
            element.set(attribute, f"{float(element.get(attribute, 0)) + delta:.6f}")

    for command in touched:
        for pair in range(len(command.values) // 2):
            command.values[pair * 2 + index] += delta
    edge.path.write()
    return None


def _valid(
    diagram: Diagram,
    baseline: dict[tuple[str, bool], float],
    baseline_skew: int,
    baseline_collisions: set,
    baseline_labels: dict[str, float],
) -> str | None:
    """Gate geometrico: restituisce la ragione del fallimento, o ``None`` se passa."""
    if image_overlaps(diagram):
        return "overlap fra icone"
    if outside_container(diagram):
        return "nodo fuori dal container"
    if outside_viewbox(diagram):
        return "nodo fuori dal viewBox"
    if non_orthogonal_segments(diagram) > baseline_skew:
        return "nuovo segmento non ortogonale"
    if collisions(diagram) - baseline_collisions:
        return "nuova collisione fra nodi, archi o label"
    for edge in diagram.edges:
        position = label_segment(edge)
        if position is not None and position[1] > baseline_labels.get(edge.edge_id, position[1]) + EPS:
            return "label allontanata dal percorso"
    for key, value in _attachments(diagram).items():
        if value > baseline.get(key, value) + EPS:
            return f"estremo staccato: {key[0][:40]}"
    return None


def _align(diagram: Diagram, report: Report) -> Diagram:
    """S3/S4: porta i nodi fuori asse sull'asse di maggioranza del cluster."""
    baseline = _attachments(diagram)
    baseline_skew = non_orthogonal_segments(diagram)
    baseline_collisions = collisions(diagram)
    baseline_labels = _label_distances(diagram)
    for cluster_id in sorted({o.cluster_id for o in find_outliers(diagram)}):
        snapshot = _serialize(diagram)
        candidates = [o for o in find_outliers(diagram) if o.cluster_id == cluster_id]
        applied = []
        for outlier in candidates:
            if not outlier.movable:
                report.skipped.append((outlier.node_id, outlier.reason))
                continue
            node = diagram.nodes[outlier.node_id]
            dx = outlier.delta if outlier.axis == "x" else 0.0
            dy = outlier.delta if outlier.axis == "y" else 0.0
            node.translate(dx, dy)
            for edge, at_source in diagram.incident(outlier.node_id):
                _shift_endpoint(edge, at_source, outlier.axis, outlier.delta)
            applied.append(f"{outlier.node_id} {outlier.axis}{outlier.delta:+.1f}")
        if not applied:
            continue
        failure = _valid(diagram, baseline, baseline_skew, baseline_collisions, baseline_labels)
        if failure is None:
            report.aligned.extend(applied)
        else:
            report.rolled_back.append(f"{cluster_id}: {failure}")
            diagram = _reload(snapshot, diagram.path)
    return diagram


def _center_ports(diagram: Diagram, report: Report) -> Diagram:
    """S5: centra il fascio di archi di un lato sul centro visibile del nodo."""
    baseline = _attachments(diagram)
    baseline_skew = non_orthogonal_segments(diagram)
    baseline_collisions = collisions(diagram)
    baseline_labels = _label_distances(diagram)
    for span in find_port_spans(diagram):
        if abs(span.offset) <= EPS:
            continue
        if any(not e.path.supported for e, _ in diagram.incident(span.node_id)):
            report.skipped.append((span.node_id, "arco incidente non supportato"))
            continue
        snapshot = _serialize(diagram)
        node = diagram.nodes[span.node_id]
        axis = "y" if span.side in ("destra", "sinistra") else "x"
        delta = -span.offset
        moved = 0
        refusal: str | None = None
        for edge, at_source in diagram.incident(span.node_id):
            terminal = edge.terminal_axis(at_source)
            point = edge.start if at_source else edge.end
            x, y, w, h = node.box
            if terminal == "x":
                side = "destra" if point[0] >= x + w / 2 else "sinistra"
            elif terminal == "y":
                side = "sotto" if point[1] >= y + h / 2 else "sopra"
            else:
                continue
            if side != span.side:
                continue
            refusal = _shift_terminal_segment(edge, at_source, delta, axis)
            if refusal is not None:
                break
            moved += 1

        failure = refusal or (
            _valid(diagram, baseline, baseline_skew, baseline_collisions, baseline_labels)
            if moved else "nessun arco spostabile"
        )
        if moved and failure is None:
            report.centered.append(f"{span.node_id} {span.side} {delta:+.1f} ({moved} archi)")
        else:
            if moved and refusal is None:
                report.rolled_back.append(f"{span.node_id}:{span.side}: {failure}")
            else:
                report.skipped.append((f"{span.node_id}:{span.side}", failure or "nessun arco"))
            diagram = _reload(snapshot, diagram.path)
    return diagram


def _label_distances(diagram: Diagram) -> dict[str, float]:
    return {e.edge_id: position[1] for e in diagram.edges
            if (position := label_segment(e)) is not None}


# ecs_hero deliberately retains its D2 dimensions (110x110 in the ECS template).
DEFAULT_ICON_SIZES = {"aws_node": 60.0}


def normalize_svg(
    source: Path,
    destination: Path | None = None,
    *,
    sizes: dict[str, float] | None = None,
    symmetry: bool = True,
) -> Report:
    """Normalize a D2 SVG with deterministic, local corrections and rollback.

    Inputs are never changed when a distinct destination is supplied. An
    unsupported coordinate transform makes this pass abstain entirely. Paths
    outside D2's M/L/S/C dialect are preserved and block incident corrections.
    Invalid XML is a render failure, not a successful unnormalized output.
    """
    destination = destination or source
    sizes = DEFAULT_ICON_SIZES if sizes is None else sizes
    if any(not math.isfinite(size) or size <= 0 for size in sizes.values()):
        raise ValueError("Icon sizes must be finite positive numbers")
    tree = ET.parse(source)
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    report = Report()
    root = tree.getroot()
    if any(e.get("transform") for e in root.iter()):
        report.skipped.append(("svg", "coordinate trasformate non supportate"))
        if destination != source:
            destination.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(destination, source.read_bytes())
        return report
    ns = "{http://www.w3.org/2000/svg}"
    for group in root.iter(f"{ns}g"):
        rules = [size for name, size in sizes.items() if name in group.get("class", "").split()]
        if len(rules) != 1:
            continue
        image = next(group.iter(f"{ns}image"), None)
        if image is None:
            continue
        x, y, w, h = (float(image.get(a, 0)) for a in ("x", "y", "width", "height"))
        if not all(math.isfinite(v) for v in (x, y, w, h)) or min(w, h) <= 0:
            raise ValueError("Invalid image geometry in SVG")
        size = rules[0]
        report.resized += int(abs(w - size) > EPS or abs(h - size) > EPS)
        for attribute, value in (("x", x + (w-size)/2), ("y", y + (h-size)/2), ("width", size), ("height", size)):
            image.set(attribute, f"{value:.6f}")
    diagram = Diagram.from_tree(tree, source)
    report.skipped.extend((e.edge_id, "percorso non supportato") for e in diagram.edges if not e.path.supported)
    if symmetry:
        diagram = _align(diagram, report)
        diagram = _center_ports(diagram, report)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    _atomic_write(destination, _serialize(diagram))
    return report


def _atomic_write(destination: Path, data: bytes) -> None:
    """Leave the preceding artifact intact if serialization or writing fails."""
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".svg-", delete=False) as f:
            name = f.name
            f.write(data)
        os.replace(name, destination)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)
