import base64
import xml.etree.ElementTree as ET
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_explicit_alignment import fixture
from pndocgen.engine.renderers.svg_geometry import Diagram, NS, edge_endpoints, collisions
from pndocgen.engine.renderers.svg_alignment import Rule
from pndocgen.engine.renderers.svg_preferences import center_by_alternatives
from pndocgen.engine.renderers.svg_labels import separate_labels


@pytest.mark.parametrize("identifier,expected", [
    ("(a.x -> b.y)[0]", ("a.x", "b.y")),
    ("ingress.(rules.a -> queues.b)[0]", ("ingress.rules.a", "ingress.queues.b")),
    ("a.b.(x -> _.y)[12]", ("a.b.x", "a.y")),
    ("plain.node", None), ("(a -> b)", None)])
def test_scoped_connection_ids(identifier, expected):
    assert edge_endpoints(identifier) == expected


def test_scoped_edge_is_incident_on_full_paths(tmp_path):
    source = fixture(tmp_path, [("ingress.rules.a", 100, 100, "rect"),
                               ("ingress.queues.b", 400, 100, "rect")])
    tree = ET.parse(source)
    group = ET.SubElement(tree.getroot(), NS+"g", {"class": base64.b64encode(b"ingress.(rules.a -> queues.b)[0]").decode()})
    ET.SubElement(group, NS+"path", d="M 112 100 L 388 100")
    tree.write(source)
    diagram = Diagram.load(source)
    assert len(diagram.edges) == 1
    assert len(diagram.incident("ingress.rules.a")) == 1
    assert len(diagram.incident("ingress.queues.b")) == 1


def grouped_fixture(tmp_path):
    return fixture(tmp_path, [("target.a", 200, 100, "rect"), ("target.b", 200, 230, "rect"),
                              ("source.a", 50, 100, "rect"), ("source.b", 50, 200, "rect")],
                   [("source.a", "target.a", "M 62 103 L 188 103"),
                    ("source.b", "target.a", "M 62 200 L 120 200 L 120 109 L 188 109")])


def test_grouped_node_moves_along_column_without_touching_paths(tmp_path):
    source = grouped_fixture(tmp_path)
    before = Diagram.load(source)
    output = tmp_path / "out.svg"
    report = center_by_alternatives(source, output, rules={"target": Rule("column")})
    after = Diagram.load(output)
    assert report["applied"][0]["delta"] == 6
    assert after.nodes["target.a"].center == (200, 106)
    assert after.nodes["target.b"].center == before.nodes["target.b"].center
    assert [e.path.element.get("d") for e in before.edges] == [e.path.element.get("d") for e in after.edges]
    repeat = tmp_path / "repeat.svg"
    center_by_alternatives(output, repeat, rules={"target": Rule("column")})
    assert output.read_bytes() == repeat.read_bytes()


@pytest.mark.parametrize("rule,limit", [(Rule("row"), 30), (Rule("grid", columns=2), 30),
                                        (Rule("column"), 1), (Rule("column", min_gap=200), 30)])
def test_grouped_moves_respect_contract_bounds_and_gaps(tmp_path, rule, limit):
    source = grouped_fixture(tmp_path)
    output = tmp_path / "out.svg"
    report = center_by_alternatives(source, output, rules={"target": rule}, max_node_shift=limit)
    assert not report["applied"]
    assert output.read_bytes() == source.read_bytes()


def label_fixture(tmp_path):
    source = fixture(tmp_path, [("a.x", 100, 100, "rect"), ("b.x", 400, 100, "rect")],
                     [("a.x", "b.x", "M 112 100 L 388 100"),
                      ("a.x", "b.x", "M 112 110 L 388 110")])
    tree = ET.parse(source)
    mask = ET.SubElement(tree.getroot(), NS+"mask", id="labels")
    paths = [e for e in tree.getroot().iter(NS+"g") if e.find(NS+"path") is not None]
    for group, y in zip(paths, [100, 110]):
        group.find(NS+"path").set("mask", "url(#labels)")
        ET.SubElement(group, NS+"text", x="250", y=str(y)).text = "label"
        ET.SubElement(mask, NS+"rect", x="220", y=str(y-7), width="60", height="14", fill="black")
    tree.write(source)
    return source


def test_label_separation_preserves_paths_and_is_idempotent(tmp_path):
    source = label_fixture(tmp_path)
    before = Diagram.load(source)
    assert collisions(before)
    output = tmp_path / "out.svg"
    report = separate_labels(source, output)
    after = Diagram.load(output)
    assert report["applied"] and not collisions(after)
    assert [e.path.element.get("d") for e in before.edges] == [e.path.element.get("d") for e in after.edges]
    repeat = tmp_path / "repeat.svg"
    separate_labels(output, repeat)
    assert repeat.read_bytes() == output.read_bytes()


def test_label_limit_leaves_original_untouched(tmp_path):
    source = label_fixture(tmp_path)
    output = tmp_path / "out.svg"
    report = separate_labels(source, output, max_shift=0)
    assert not report["applied"] and report["skipped"]
    assert output.read_bytes() == source.read_bytes()
