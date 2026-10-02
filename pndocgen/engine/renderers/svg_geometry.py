"""Modello geometrico dell'SVG prodotto da D2, per misura e simmetrizzazione.

Solo stdlib. Nessuna chiamata AWS o di rete: opera su file gia' su disco.

Nell'SVG di D2 ogni gruppo porta in ``class`` l'id del nodo codificato in
base64; per gli archi l'id decodifica a ``(source -> target)[indice]``, quindi
il join fra archi e nodi e' esatto e non euristico. I percorsi sono polilinee
ortogonali ``M``/``L`` con angoli arrotondati resi da un comando ``S``.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

NS = "{http://www.w3.org/2000/svg}"

EPS = 0.6
"""Tolleranza di uguaglianza fra coordinate, sotto il pixel."""

ICON_HALF = 30.0
"""Mezza icona: oltre questo scarto non e' disallineamento ma colonna diversa."""

CORNER = 10.0
"""Raggio dell'angolo arrotondato usato da D2 nei path."""

_TOKEN = re.compile(r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?")


def parse_path(value: str) -> list[Command]:
    """Accept only the absolute M/L/S/C dialect emitted by the pinned D2 version.

    Unknown commands must not be silently discarded when rewriting a path.
    An empty result marks the entire path unsupported; callers abstain.
    """
    tokens = _TOKEN.findall(value)
    if _TOKEN.sub("", value).replace(",", "").strip():
        return []
    commands = []
    i = 0
    while i < len(tokens):
        letter = tokens[i]
        arity = {"M": 2, "L": 2, "S": 4, "C": 6}.get(letter)
        if arity is None or i + arity >= len(tokens):
            return []
        try:
            values = [float(v) for v in tokens[i + 1:i + 1 + arity]]
        except ValueError:
            return []
        if not all(math.isfinite(v) for v in values):
            return []
        commands.append(Command(letter, values))
        i += 1 + arity
    if len(commands) < 2 or commands[0].letter != "M":
        return []
    if any(c.letter == "M" for c in commands[1:]):
        return []
    return commands


def decode_id(class_attr: str) -> str | None:
    """Restituisce l'id D2 codificato nel primo token di ``class``."""
    tokens = class_attr.split()
    if not tokens:
        return None
    token = tokens[0]
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=True)
        return raw.decode("utf-8").replace("-&gt;", "->")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def edge_endpoints(identifier: str) -> tuple[str, str] | None:
    """Resolve D2 SVG connection IDs, including a common ancestor scope."""
    match = re.fullmatch(r"(?P<scope>(?:[^()]+\.)?)\((?P<source>.+) -> (?P<target>.+)\)\[\d+\]", identifier)
    if match is None:
        return None
    def absolute(value):
        parts = match["scope"].rstrip(".").split(".") if match["scope"] else []
        for part in value.strip().split("."):
            if part == "_":
                if not parts:
                    raise ValueError("Invalid D2 parent scope in connection ID")
                parts.pop()
            else:
                parts.append(part)
        return ".".join(parts)
    return absolute(match["source"]), absolute(match["target"])


@dataclass
class Command:
    letter: str
    values: list[float]

    @property
    def anchor(self) -> tuple[float, float]:
        return self.values[-2], self.values[-1]


@dataclass
class EdgePath:
    element: ET.Element
    commands: list[Command]

    @property
    def supported(self) -> bool:
        return bool(self.commands)

    @property
    def anchors(self) -> list[tuple[float, float]]:
        return [c.anchor for c in self.commands if c.letter != "Z"]

    def write(self) -> None:
        parts = [
            c.letter + " " + " ".join(f"{v:.6f}" for v in c.values)
            for c in self.commands
        ]
        self.element.set("d", " ".join(parts))


@dataclass
class Node:
    node_id: str
    group: ET.Element
    image: ET.Element | None
    rect: ET.Element | None
    texts: list[ET.Element]
    classes: list[str]

    @property
    def is_container(self) -> bool:
        return "boundary" in self.classes

    @property
    def cluster_id(self) -> str | None:
        return self.node_id.rsplit(".", 1)[0] if "." in self.node_id else None

    @property
    def box(self) -> tuple[float, float, float, float]:
        """Bounding box visibile: immagine per i nodi, rettangolo per i cluster."""
        shape = self.image if self.image is not None else self.rect
        assert shape is not None
        return (
            float(shape.get("x", 0)),
            float(shape.get("y", 0)),
            float(shape.get("width", 0)),
            float(shape.get("height", 0)),
        )

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.box
        return x + w / 2, y + h / 2

    def translate(self, dx: float, dy: float) -> None:
        # Element ha __len__ pari al numero di figli: va testato con "is not None".
        shapes = [e for e in (self.image, self.rect, *self.texts) if e is not None]
        for element in shapes:
            element.set("x", f"{float(element.get('x', 0)) + dx:.6f}")
            element.set("y", f"{float(element.get('y', 0)) + dy:.6f}")


@dataclass
class Edge:
    edge_id: str
    source: str
    target: str
    path: EdgePath
    label: ET.Element | None
    label_mask: ET.Element | None = None

    @property
    def start(self) -> tuple[float, float]:
        return self.path.anchors[0]

    @property
    def end(self) -> tuple[float, float]:
        return self.path.anchors[-1]

    def terminal(self, at_source: bool) -> tuple[tuple[float, float], tuple[float, float]]:
        """Segmento terminale come coppia (estremo, punto successivo verso l'interno)."""
        anchors = self.path.anchors
        return (anchors[0], anchors[1]) if at_source else (anchors[-1], anchors[-2])

    def terminal_axis(self, at_source: bool) -> str | None:
        """``x`` se il segmento terminale e' orizzontale, ``y`` se verticale."""
        if not self.path.supported:
            return None
        (x0, y0), (x1, y1) = self.terminal(at_source)
        if abs(y1 - y0) <= EPS and abs(x1 - x0) > EPS:
            return "x"
        if abs(x1 - x0) <= EPS and abs(y1 - y0) > EPS:
            return "y"
        return None


@dataclass
class Diagram:
    path: Path
    tree: ET.ElementTree
    nodes: dict[str, Node] = field(default_factory=dict)
    containers: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    viewbox: tuple[float, float, float, float] = (0, 0, 0, 0)

    @classmethod
    def load(cls, path: Path) -> "Diagram":
        return cls.from_tree(ET.parse(path), path)

    @classmethod
    def from_tree(cls, tree: ET.ElementTree, path: Path) -> "Diagram":
        root = tree.getroot()
        box = [float(v) for v in root.get("viewBox", "0 0 0 0").split()]
        diagram = cls(path=path, tree=tree, viewbox=(box[0], box[1], box[2], box[3]))

        for group in root.iter(f"{NS}g"):
            decoded = decode_id(group.get("class", ""))
            if decoded is None:
                continue
            if edge_endpoints(decoded) is not None:
                diagram._add_edge(decoded, group)
            else:
                diagram._add_node(decoded, group)
        return diagram

    def _add_node(self, node_id: str, group: ET.Element) -> None:
        shape = next(
            (g for g in group.iter(f"{NS}g") if "shape" in g.get("class", "").split()),
            None,
        )
        if shape is None:
            return
        image = next(shape.iter(f"{NS}image"), None)
        rect = next(shape.iter(f"{NS}rect"), None)
        if image is None and rect is None:
            return
        node = Node(
            node_id=node_id,
            group=group,
            image=image,
            rect=rect if image is None else None,
            texts=[t for t in group.iter(f"{NS}text")],
            classes=group.get("class", "").split()[1:],
        )
        (self.containers if node.is_container else self.nodes)[node_id] = node

    def _add_edge(self, edge_id: str, group: ET.Element) -> None:
        element = next(group.iter(f"{NS}path"), None)
        if element is None:
            return
        endpoints = edge_endpoints(edge_id)
        if endpoints is None:
            return
        source, target = endpoints
        commands = parse_path(element.get("d", ""))
        label = next(group.iter(f"{NS}text"), None)
        label_mask = None
        mask_ref = element.get("mask", "")
        if label is not None and mask_ref.startswith("url(#"):
            mask_id = mask_ref[5:-1]
            masks = [m for m in self.tree.getroot().iter(f"{NS}mask") if m.get("id") == mask_id]
            matches = []
            for mask in masks:
                for rect in mask.iter(f"{NS}rect"):
                    if rect.get("fill") != "black":
                        continue
                    x, y, w, h = rect_box(rect)
                    if (abs(x + w / 2 - float(label.get("x", 0))) <= EPS
                            and y <= float(label.get("y", 0)) <= y + h):
                        matches.append(rect)
            if len(matches) == 1:
                label_mask = matches[0]
        self.edges.append(
            Edge(
                edge_id=edge_id,
                source=source,
                target=target,
                path=EdgePath(element, commands),
                label=label,
                label_mask=label_mask,
            )
        )

    def incident(self, node_id: str) -> list[tuple[Edge, bool]]:
        """Archi incidenti sul nodo, con ``True`` se il nodo e' la sorgente."""
        return [
            (edge, at_source)
            for edge in self.edges
            for at_source in (True, False)
            if (edge.source if at_source else edge.target) == node_id
        ]

    def children_of(self, cluster_id: str) -> list[Node]:
        return sorted(
            (n for n in self.nodes.values() if n.cluster_id == cluster_id),
            key=lambda n: n.node_id,
        )

    def save(self, path: Path) -> None:
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        self.tree.write(path, encoding="utf-8", xml_declaration=True)


# --------------------------------------------------------------------------
# Misure
# --------------------------------------------------------------------------


def group_values(values: list[float], tolerance: float) -> list[list[float]]:
    """Raggruppa valori vicini entro ``tolerance``, in ordine crescente."""
    groups: list[list[float]] = []
    for value in sorted(values):
        if groups and value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return groups


def majority(values: list[float]) -> float | None:
    """Valore piu' frequente, solo se supera stretta meta' del gruppo."""
    counts: dict[float, int] = {}
    for value in values:
        counts[round(value, 3)] = counts.get(round(value, 3), 0) + 1
    best, count = max(counts.items(), key=lambda item: (item[1], -item[0]))
    return best if count * 2 > len(values) else None


@dataclass
class Outlier:
    cluster_id: str
    node_id: str
    axis: str
    delta: float
    incident: int
    movable: bool
    reason: str


def find_outliers(diagram: Diagram) -> list[Outlier]:
    """Nodi il cui centro e' fuori dall'asse di maggioranza del proprio cluster."""
    outliers: list[Outlier] = []
    for cluster_id in sorted({n.cluster_id for n in diagram.nodes.values() if n.cluster_id}):
        children = diagram.children_of(cluster_id)
        if len(children) < 3:
            continue
        centers = {node.node_id: node.center for node in children}
        distinct_x = len(group_values([c[0] for c in centers.values()], ICON_HALF))
        distinct_y = len(group_values([c[1] for c in centers.values()], ICON_HALF))
        if distinct_x == distinct_y:
            continue
        # L'asse di impilamento e' quello con piu' valori distinti: non si tocca.
        axis = "x" if distinct_y > distinct_x else "y"
        index = 0 if axis == "x" else 1

        values = [centers[node.node_id][index] for node in children]
        for lane in group_values(values, ICON_HALF):
            if len(lane) < 3:
                continue
            target = majority(lane)
            if target is None:
                continue
            for node in children:
                value = centers[node.node_id][index]
                if value not in lane or abs(value - target) <= EPS:
                    continue
                delta = target - value
                incident = diagram.incident(node.node_id)
                movable, reason = _movable(diagram, node, incident, axis, delta)
                outliers.append(
                    Outlier(cluster_id, node.node_id, axis, delta, len(incident), movable, reason)
                )
    return outliers


def _movable(
    diagram: Diagram,
    node: Node,
    incident: list[tuple[Edge, bool]],
    axis: str,
    delta: float,
) -> tuple[bool, str]:
    """Un nodo e' spostabile se ogni arco incidente si limita ad allungarsi."""
    if abs(delta) > ICON_HALF:
        return False, "scarto oltre mezza icona"
    for edge, at_source in incident:
        terminal = edge.terminal_axis(at_source)
        if terminal is None:
            return False, "segmento terminale non ortogonale"
        if terminal != axis:
            return False, "segmento terminale perpendicolare allo spostamento"
        (a0, b0), (a1, b1) = edge.terminal(at_source)
        start, other = (a0, a1) if axis == "x" else (b0, b1)
        old = other - start
        new = other - (start + delta)
        if old * new <= 0 or abs(new) < CORNER:
            return False, "segmento terminale troppo corto dopo lo spostamento"
    container = diagram.containers.get(node.cluster_id or "")
    if container is not None:
        x, y, w, h = node.box
        cx, cy, cw, ch = container.box
        if axis == "x" and not (cx <= x + delta and x + delta + w <= cx + cw):
            return False, "uscirebbe dal container"
        if axis == "y" and not (cy <= y + delta and y + delta + h <= cy + ch):
            return False, "uscirebbe dal container"
    return True, ""


@dataclass
class PortSpan:
    node_id: str
    side: str
    count: int
    offset: float


def find_port_spans(diagram: Diagram) -> list[PortSpan]:
    """Scostamento fra il centro del fascio di archi e il centro visibile del nodo."""
    spans: list[PortSpan] = []
    for node_id, node in sorted(diagram.nodes.items()):
        x, y, w, h = node.box
        sides: dict[str, list[float]] = {}
        for edge, at_source in diagram.incident(node_id):
            axis = edge.terminal_axis(at_source)
            if axis is None:
                continue
            point = edge.start if at_source else edge.end
            if axis == "x":
                side = "destra" if point[0] >= x + w / 2 else "sinistra"
                sides.setdefault(side, []).append(point[1])
            else:
                side = "sotto" if point[1] >= y + h / 2 else "sopra"
                sides.setdefault(side, []).append(point[0])
        for side, values in sorted(sides.items()):
            if len(values) < 2:
                continue
            span_center = (min(values) + max(values)) / 2
            visible = y + h / 2 if side in ("destra", "sinistra") else x + w / 2
            spans.append(PortSpan(node_id, side, len(values), span_center - visible))
    return spans


def image_overlaps(diagram: Diagram) -> int:
    boxes = [n.box for n in diagram.nodes.values() if n.image is not None]
    return sum(
        1
        for i, (x1, y1, w1, h1) in enumerate(boxes)
        for x2, y2, w2, h2 in boxes[i + 1:]
        if x1 < x2 + w2 and x2 < x1 + w1 and y1 < y2 + h2 and y2 < y1 + h1
    )


def non_orthogonal_segments(diagram: Diagram) -> int:
    """Segmenti rettilinei non allineati agli assi; gli angoli ``S`` sono esclusi."""
    count = 0
    for edge in diagram.edges:
        previous: tuple[float, float] | None = None
        for command in edge.path.commands:
            if command.letter == "Z":
                continue
            if command.letter == "L" and previous is not None:
                (x0, y0), (x1, y1) = previous, command.anchor
                if abs(x1 - x0) > EPS and abs(y1 - y0) > EPS:
                    count += 1
            previous = command.anchor
    return count


def outside_viewbox(diagram: Diagram) -> int:
    left, top, width, height = diagram.viewbox
    count = 0
    for node in diagram.nodes.values():
        x, y, w, h = node.box
        if x < left or y < top or x + w > left + width or y + h > top + height:
            count += 1
    return count


def outside_container(diagram: Diagram) -> int:
    count = 0
    for node in diagram.nodes.values():
        container = diagram.containers.get(node.cluster_id or "")
        if container is None:
            continue
        x, y, w, h = node.box
        cx, cy, cw, ch = container.box
        if x < cx - EPS or y < cy - EPS or x + w > cx + cw + EPS or y + h > cy + ch + EPS:
            count += 1
    return count


def icon_sizes(diagram: Diagram) -> set[tuple[float, float]]:
    return {
        (node.box[2], node.box[3])
        for node in diagram.nodes.values()
        if node.image is not None and "aws_node" in node.classes
    }


def rect_box(element: ET.Element) -> tuple[float, float, float, float]:
    return tuple(float(element.get(a, 0)) for a in ("x", "y", "width", "height"))


def straight_segments(edge: Edge):
    """Yield (command index, start, end) for straight parts, excluding corners."""
    for i, command in enumerate(edge.path.commands[1:], 1):
        if command.letter == "L":
            yield i, edge.path.commands[i - 1].anchor, command.anchor


def point_segment_distance(point, start, end) -> float:
    px, py = point
    ax, ay = start
    bx, by = end
    length2 = (bx - ax) ** 2 + (by - ay) ** 2
    t = max(0.0, min(1.0, ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / length2)) if length2 else 0.0
    return math.hypot(px - ax - t * (bx - ax), py - ay - t * (by - ay))


def label_segment(edge: Edge) -> tuple[int, float] | None:
    """Locate an edge label using its exact D2 mask rectangle, not font estimates."""
    if edge.label_mask is None or not edge.path.supported:
        return None
    x, y, w, h = rect_box(edge.label_mask)
    choices = [(point_segment_distance((x + w / 2, y + h / 2), a, b), i)
               for i, a, b in straight_segments(edge)]
    if not choices:
        return None
    distance, index = min(choices)
    return index, distance


def _intersects(a, b) -> bool:
    x, y, w, h = a
    xx, yy, ww, hh = b
    return min(x + w, xx + ww) - max(x, xx) > EPS and min(y + h, yy + hh) - max(y, yy) > EPS


def _segment_hits_box(start, end, box) -> bool:
    x, y, w, h = box
    if abs(start[1] - end[1]) <= EPS:
        return y + EPS < start[1] < y + h - EPS and min(max(start[0], end[0]), x + w) - max(min(start[0], end[0]), x) > EPS
    if abs(start[0] - end[0]) <= EPS:
        return x + EPS < start[0] < x + w - EPS and min(max(start[1], end[1]), y + h) - max(min(start[1], end[1]), y) > EPS
    return False


def collisions(diagram: Diagram) -> set[tuple[str, str, str]]:
    """Conflicts in visible node boxes and exact edge-label masks.

    Straight edge segments are checked against nonincident nodes. This is not
    a font renderer or a full Bezier/label collision detector.
    """
    issues = set()
    nodes = list(diagram.nodes.values())
    labels = [(e.edge_id, rect_box(e.label_mask)) for e in diagram.edges if e.label_mask is not None]
    for i, node in enumerate(nodes):
        for other in nodes[i + 1:]:
            if _intersects(node.box, other.box):
                issues.add(("node-node", *sorted((node.node_id, other.node_id))))
        for edge_id, box in labels:
            if _intersects(node.box, box):
                issues.add(("label-node", edge_id, node.node_id))
        for edge in diagram.edges:
            if node.node_id in (edge.source, edge.target):
                continue
            if any(_segment_hits_box(a, b, node.box) for _, a, b in straight_segments(edge)):
                issues.add(("edge-node", edge.edge_id, node.node_id))
    for i, (edge_id, box) in enumerate(labels):
        for other_id, other_box in labels[i + 1:]:
            if _intersects(box, other_box):
                issues.add(("label-label", *sorted((edge_id, other_id))))
    return issues
