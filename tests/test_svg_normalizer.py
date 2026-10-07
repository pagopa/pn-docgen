"""Offline regression against frozen 007 artifacts and synthetic geometries."""

import copy
import base64
import json
import random
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pndocgen.engine.core.models import Component, ComponentGraph, Edge, InventoryNode
from pndocgen.engine.renderers.d2_generator import D2Generator
from pndocgen.engine.renderers.svg_geometry import (
    Command, Diagram, Edge as SvgEdge, EdgePath, Node, _movable, collisions,
    find_outliers, find_port_spans, icon_sizes, label_segment, non_orthogonal_segments,
    outside_container, outside_viewbox, parse_path, rect_box,
)
from pndocgen.engine.renderers.svg_normalizer import normalize_svg

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "experiments/007-symmetry/out/baseline"
TEMPLATES = ROOT / "pndocgen/engine/d2/templates"
FIXTURES = {
    "pn_delivery_simplified": ("pn_delivery/pn_delivery_202602241056_graph.json", "simplified", 0),
    "pn_delivery_detailed": ("pn_delivery/pn_delivery_202602241109_graph.json", "detailed", 0),
    "pn_mandate_detailed": ("pn_mandate/pn_mandate_202603060851_graph.json", "detailed", 1),
    "pn_radd_alt_simplified": ("pn_radd_alt/pn_radd_alt_202603041130_graph.json", "simplified", 0),
    "pn_radd_alt_detailed": ("pn_radd_alt/pn_radd_alt_202603041142_graph.json", "detailed", 1),
}


def load_graph(path):
    data = json.loads(path.read_text())
    graph = ComponentGraph()
    for name, item in data["components"].items():
        component = Component(name, item.get("account", ""))
        for cluster, nodes in item["clusters"].items():
            for n in nodes:
                component.add_node(cluster, InventoryNode(n["id"], n["name"], n["type"], component.account, ""))
        graph.add_component(component)
    graph.edges = [Edge(e["source"], e["target"], e["type"], e.get("label", "")) for e in data["edges"]]
    graph.canonicalize()
    return graph


def signature(path):
    tree = ET.parse(path)
    attrs = ("x", "y", "width", "height", "d", "transform", "points")
    return tree.getroot().get("viewBox"), [
        (e.tag, tuple(e.get(a) for a in attrs), e.text)
        for e in tree.iter() if e.tag.rsplit("}", 1)[-1] in ("image", "rect", "path", "text", "polygon")
    ]


@pytest.mark.parametrize("fixture", FIXTURES)
@pytest.mark.private
def test_frozen_svg_normalization(fixture, tmp_path):
    src = next((BASELINE / fixture).glob("*.svg"))
    original = src.read_bytes()
    before = Diagram.load(src)
    target = tmp_path / "normalized.svg"
    report = normalize_svg(src, target)
    after = Diagram.load(target)
    assert not find_outliers(after)
    assert sum(abs(s.offset) > 0.6 for s in find_port_spans(after)) == FIXTURES[fixture][2]
    assert before.viewbox == after.viewbox
    assert icon_sizes(before) == icon_sizes(after) == {(60.0, 60.0)}
    assert not (collisions(after) - collisions(before))
    assert not outside_container(after) and not outside_viewbox(after)
    assert non_orthogonal_segments(after) <= non_orthogonal_segments(before)
    assert [len(e.path.commands) for e in before.edges] == [len(e.path.commands) for e in after.edges]
    assert not report.rolled_back
    for old, new in zip(before.edges, after.edges):
        assert old.path.supported and new.path.supported
        assert [c.letter for c in old.path.commands] == [c.letter for c in new.path.commands]
        old_label, new_label = label_segment(old), label_segment(new)
        if old_label is not None:
            assert new_label[1] <= old_label[1] + 0.6
            assert float(new.label.get("y")) - float(old.label.get("y")) == pytest.approx(
                rect_box(new.label_mask)[1] - rect_box(old.label_mask)[1], abs=1e-5)
    once = target.read_bytes()
    again = normalize_svg(target)
    assert target.read_bytes() == once
    assert not again.aligned and not again.centered
    assert src.read_bytes() == original


@pytest.mark.parametrize("fixture", FIXTURES)
@pytest.mark.private
@pytest.mark.legacy_golden
def test_generator_matches_normalized_frozen_geometry(fixture, tmp_path):
    from pndocgen.engine.core.normalization_config import NormalizationConfig
    relative, detail, _ = FIXTURES[fixture]
    graph = load_graph(ROOT / "docs/generated" / relative)
    expected = tmp_path / "expected.svg"
    normalize_svg(next((BASELINE / fixture).glob("*.svg")), expected)
    generator = D2Generator(TEMPLATES, normalization=NormalizationConfig(enabled=False), ingress_layout="legacy")
    d2 = tmp_path / "actual.d2"
    generator.generate_l3(next(iter(graph.components.values())), d2, edges=graph.edges, detail_level=detail)
    assert signature(generator.render(d2, "svg")) == signature(expected)


@pytest.mark.parametrize("theme", ["white", "pastel"])
@pytest.mark.parametrize("fixture", FIXTURES)
@pytest.mark.private
@pytest.mark.legacy_golden
def test_integrated_layout_matches_approved_009(fixture, theme, tmp_path):
    from pndocgen.engine.renderers.svg_layout import normalize_layout, read_contracts
    relative, detail, _ = FIXTURES[fixture]
    graph = load_graph(ROOT / "docs/generated" / relative)
    expected = ROOT / "experiments/009-constraint-preferences/runs/001" / theme / fixture / "first/final.svg"
    generator = D2Generator(TEMPLATES, render_theme=theme, ingress_layout="legacy")
    d2 = tmp_path / "actual.d2"
    generator.generate_l3(next(iter(graph.components.values())), d2, edges=graph.edges, detail_level=detail)
    actual = generator.render(d2)
    assert signature(actual) == signature(expected)
    diagram = Diagram.load(actual)
    assert not [s for s in find_port_spans(diagram) if abs(s.offset) > 0.6]
    repeated = tmp_path / "repeat.svg"
    normalize_layout(actual, repeated, read_contracts(d2), generator.normalization)
    assert repeated.read_bytes() == actual.read_bytes()


@pytest.mark.private
def test_historical_order_and_permutations(tmp_path):
    generator = D2Generator(TEMPLATES)
    outputs = []
    for i, ts in enumerate(("202603041152", "202603051738")):
        graph = load_graph(ROOT / "docs/generated/pn_mandate" / f"pn_mandate_{ts}_graph.json")
        d2 = tmp_path / f"mandate-{i}.d2"
        generator.generate_l3(next(iter(graph.components.values())), d2, edges=graph.edges, detail_level="detailed")
        outputs.append(generator.render(d2, "svg").read_bytes())
    assert outputs[0] == outputs[1]
    for relative, _, _ in FIXTURES.values():
        graph = load_graph(ROOT / "docs/generated" / relative)
        reference = graph.to_dict()
        for seed in range(20):
            shuffled = copy.deepcopy(graph)
            rng = random.Random(seed)
            rng.shuffle(shuffled.edges)
            for component in shuffled.components.values():
                items = list(component.clusters.items())
                rng.shuffle(items)
                component.clusters = dict(items)
                for cluster in component.clusters.values():
                    rng.shuffle(cluster.nodes)
            shuffled.canonicalize()
            assert shuffled.to_dict() == reference


@pytest.mark.parametrize("delta,other,allowed", [(5, 142, False), (5, 160, True), (-5, 142, True), (20, 142, False)])
def test_terminal_length_guard(delta, other, allowed):
    root = ET.Element("svg")
    node = Node("queues.test", root, ET.Element("image", x="70", y="0", width="60", height="60"), None, [], ["aws_node"])
    path = EdgePath(ET.Element("path"), [Command("M", [130, 30]), Command("L", [other, 30])])
    edge = SvgEdge("test", node.node_id, "ecs.test", path, None)
    diagram = Diagram(Path("unused"), ET.ElementTree(root), nodes={node.node_id: node})
    assert _movable(diagram, node, [(edge, True)], "x", delta)[0] is allowed


@pytest.mark.parametrize("path", ["M 0 0 Q 3 4 5 6", "m 0 0 l 5 6", "M 0 0 L 1", "M 0 0 L nan 5"])
def test_unsupported_paths_are_not_partially_parsed(path):
    assert parse_path(path) == []


def test_cubic_control_points_are_preserved():
    commands = parse_path("M 0 0 L 10 0 C 12 0 10 4 12 4 L 20 4")
    assert [c.letter for c in commands] == ["M", "L", "C", "L"]
    assert commands[2].values == [12, 0, 10, 4, 12, 4]


@pytest.mark.parametrize("theme", ["white", "pastel"])
@pytest.mark.parametrize("detail", ["simplified", "detailed"])
def test_additional_resource_shapes(theme, detail, tmp_path):
    component = Component("test-service", "test")
    def add(cluster, name, resource_type):
        component.add_node(cluster, InventoryNode(f"test://{name}", name, resource_type, "test", "local"))
    add("ecs", "test-service", "ecs_service")
    add("queues", "events-topic", "sns")  # Current resolver maps SNS to the queues cluster.
    add("lambdas", "worker", "lambda")
    edges = [Edge("events-topic", "worker", "sns_subscription", "fan-out")]
    for i in range(4):
        add("storage", f"bucket-{i}", "s3")
        add("queues", f"output-{i}", "sqs")
        edges.extend([Edge("test-service", f"bucket-{i}", "storage_access", "reads"),
                      Edge("test-service", f"output-{i}", "sqs_producer", "produces")])
    for i in range(2):
        add("external", f"dependency-{i}", "external_service")
        edges.append(Edge("test-service", f"dependency-{i}", "http_call", "HTTP"))
    component.canonicalize()
    generator = D2Generator(TEMPLATES, theme)
    path = tmp_path / "synthetic.d2"
    generator.generate_l3(component, path, edges=edges, detail_level=detail)
    d = Diagram.load(generator.render(path, "svg"))
    assert len(d.nodes) == 13
    assert icon_sizes(d) == {(60.0, 60.0)}
    assert not outside_container(d) and not outside_viewbox(d)
    if detail == "simplified":
        assert "output_queues" in d.containers
    assert "storage" in d.containers and "external" in d.containers


def test_invalid_svg_leaves_destination_intact(tmp_path):
    source = tmp_path / "invalid.svg"
    target = tmp_path / "existing.svg"
    source.write_text("<svg>")
    target.write_text("previous artifact")
    with pytest.raises(ET.ParseError):
        normalize_svg(source, target)
    assert target.read_text() == "previous artifact"


def _collision_fixture(tmp_path, *, unsupported=False, transform=False):
    ns = "{http://www.w3.org/2000/svg}"
    root = ET.Element(ns + "svg", viewBox="0 0 400 400")
    if transform:
        root.set("transform", "translate(10 20)")
    def group(identifier, classes=""):
        encoded = base64.b64encode(identifier.encode()).decode()
        return ET.SubElement(root, ns + "g", {"class": encoded + " " + classes})
    for name, x, y in (("src", 0, 0), ("dst1", 200, 120), ("dst2", 200, 220)):
        shape = ET.SubElement(group(name, "aws_node"), ns + "g", {"class": "shape"})
        ET.SubElement(shape, ns + "image", x=str(x), y=str(y), width="60", height="60")
    shape = ET.SubElement(group("obstacle"), ns + "g", {"class": "shape"})
    ET.SubElement(shape, ns + "rect", x="80", y="10", width="10", height="20")
    paths = ["M 62 40 L 100 40 S 110 40 110 50 L 110 140 S 110 150 120 150 L 198 150",
             "M 62 50 L 140 50 S 150 50 150 60 L 150 240 S 150 250 160 250 L 198 250"]
    if unsupported:
        paths[0] = "M 62 40 Q 100 40 198 150"
    for i, path in enumerate(paths, 1):
        ET.SubElement(group(f"(src -> dst{i})[0]"), ns + "path", d=path)
    source = tmp_path / "source.svg"
    ET.ElementTree(root).write(source)
    return source


def test_collision_rolls_back_the_entire_bundle(tmp_path):
    source = _collision_fixture(tmp_path)
    before = Diagram.load(source)
    target = tmp_path / "normalized.svg"
    report = normalize_svg(source, target)
    after = Diagram.load(target)
    assert report.rolled_back
    assert not report.centered
    assert [e.path.element.get("d") for e in after.edges] == [e.path.element.get("d") for e in before.edges]
    assert collisions(after) == collisions(before)


def test_unsupported_path_is_preserved_and_reported(tmp_path):
    source = _collision_fixture(tmp_path, unsupported=True)
    before = Diagram.load(source)
    target = tmp_path / "normalized.svg"
    report = normalize_svg(source, target)
    after = Diagram.load(target)
    assert any("percorso non supportato" in reason for _, reason in report.skipped)
    assert after.edges[0].path.element.get("d") == before.edges[0].path.element.get("d")


def test_transformed_coordinates_are_preserved_verbatim(tmp_path):
    source = _collision_fixture(tmp_path, transform=True)
    target = tmp_path / "normalized.svg"
    report = normalize_svg(source, target)
    assert report.skipped
    assert target.read_bytes() == source.read_bytes()
