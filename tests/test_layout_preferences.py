import importlib.util
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pndocgen.engine.renderers.svg_preferences import base, prefer_free_nodes, center_by_alternatives, _candidate
ROOT = Path(__file__).resolve().parents[1]
from pndocgen.engine.renderers.svg_geometry import Diagram, find_port_spans

spec = importlib.util.spec_from_file_location("fixtures_008", Path(__file__).with_name("test_explicit_alignment.py"))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


@pytest.mark.parametrize("layout", ["column", "row"])
def test_free_node_moves_and_attached_node_path_stays_identical(tmp_path, layout):
    nodes = [("group.free", 118, 230, "rect"), ("group.fixed", 100, 100, "rect"),
             ("other.a", 400, 100, "rect")]
    path = "M 112 100 L 388 100"
    if layout == "row":
        nodes = [(name, y, x, shape) for name, x, y, shape in nodes]
        path = "M 100 112 L 100 388"
    source = helpers.fixture(tmp_path, nodes, [("group.fixed", "other.a", path)])
    target = tmp_path / "out.svg"
    report = prefer_free_nodes(source, target, {"group": base.Rule(layout)})
    d = Diagram.load(target)
    assert d.nodes["group.fixed"].center == (100, 100)
    axis = 0 if layout == "column" else 1
    assert d.nodes["group.free"].center[axis] == 100
    assert d.edges[0].path.element.get("d") == path
    assert len(report["applied"][0]["moves"]) == 1


def test_conflicting_attached_axes_are_not_given_an_arbitrary_priority(tmp_path):
    source = helpers.fixture(tmp_path, [("group.a", 100, 100, "rect"), ("group.b", 118, 200, "rect"),
                                        ("group.free", 140, 300, "rect"), ("other.a", 400, 100, "rect")],
                             [("group.a", "other.a", "M 112 100 L 388 100"),
                              ("group.b", "other.a", "M 130 200 L 388 200")])
    target = tmp_path / "out.svg"
    report = prefer_free_nodes(source, target, {"group": base.Rule("column")})
    assert not report["applied"]
    assert target.read_bytes() == source.read_bytes()


def test_no_cascade_for_grouped_nodes(tmp_path):
    source = helpers.fixture(tmp_path, [("group.a", 100, 100, "rect"), ("group.b", 100, 230, "rect"),
                                        ("other.a", 400, 100, "rect"), ("other.b", 400, 150, "rect")],
                             [("group.a", "other.a", "M 112 103 L 388 103"),
                              ("group.a", "other.b", "M 112 109 L 388 109")])
    target = tmp_path / "out.svg"
    report = center_by_alternatives(source, target)
    assert any(item["reason"] == "non-singleton container" for item in report["skipped"])
    assert target.read_bytes() == source.read_bytes()


def test_impossible_straight_bundles_are_preserved(tmp_path):
    source = helpers.fixture(tmp_path, [("hero.a", 200, 100, "rect"),
                                        ("left.a", 50, 100, "rect"), ("left.b", 50, 150, "rect"),
                                        ("right.a", 400, 100, "rect"), ("right.b", 400, 150, "rect")],
                             [("left.a", "hero.a", "M 62 101 L 188 101"),
                              ("left.b", "hero.a", "M 62 109 L 188 109"),
                              ("hero.a", "right.a", "M 212 96 L 388 96"),
                              ("hero.a", "right.b", "M 212 104 L 388 104")])
    target = tmp_path / "out.svg"
    report = center_by_alternatives(source, target)
    assert not report["applied"]
    assert any(item["reason"] == "no candidate passed every gate" for item in report["skipped"])
    assert target.read_bytes() == source.read_bytes()


def test_lambda_single_moves_to_fixed_input_and_output_ports(tmp_path):
    source = ROOT / "tests/fixtures/lambda_single_unbalanced.svg"
    original = Diagram.load(source)
    node_id = "lambdas.stress_lambda_single_lambda_00"
    assert [s.offset for s in find_port_spans(original) if s.node_id == node_id] == [9.0]
    old_paths = {edge.edge_id: edge.path.element.get("d") for edge in original.edges}

    target = tmp_path / "centered.svg"
    report = center_by_alternatives(source, target)
    final = Diagram.load(target)
    assert report["applied"] == [{"node": node_id, "axis": "y", "delta": 9.0,
                                  "changed_paths": 0}]
    assert final.nodes[node_id].center[1] == original.nodes[node_id].center[1] + 9.0
    assert not [s for s in find_port_spans(final) if s.node_id == node_id and abs(s.offset) > 0.6]
    assert {edge.edge_id: edge.path.element.get("d") for edge in final.edges} == old_paths

    again = tmp_path / "again.svg"
    assert not center_by_alternatives(target, again)["applied"]
    assert again.read_bytes() == target.read_bytes()


@pytest.mark.parametrize("fixture", ["pn_mandate_detailed", "pn_radd_alt_detailed"])
@pytest.mark.private
def test_real_hero_fallback_preserves_incoming_paths_and_centers_both_sides(tmp_path, fixture):
    source = ROOT / f"experiments/008-explicit-alignment/runs/002/white/{fixture}/after.svg"
    original = Diagram.load(source)
    target = tmp_path / "out.svg"
    report = center_by_alternatives(source, target)
    assert len(report["applied"]) == 1
    final = Diagram.load(target)
    node_id = report["applied"][0]["node"]
    assert not [s for s in find_port_spans(final) if s.node_id == node_id and abs(s.offset) > 0.6]
    old_incoming = {e.edge_id: e.path.element.get("d") for e, src in original.incident(node_id) if not src}
    assert all(e.path.element.get("d") == old_incoming[e.edge_id] for e in final.edges if e.edge_id in old_incoming)
    center_by_alternatives(target, tmp_path / "again.svg")
    assert target.read_bytes() == (tmp_path / "again.svg").read_bytes()
    with pytest.raises(FileExistsError):
        center_by_alternatives(source, target)


@pytest.mark.private
def test_candidate_collision_rolls_back(tmp_path):
    source = ROOT / "experiments/008-explicit-alignment/runs/002/white/pn_mandate_detailed/after.svg"
    d = Diagram.load(source)
    hero = next(n for n in d.nodes.values() if n.node_id.startswith("ecs."))
    obstacle = next(n for n in d.nodes.values() if n.node_id.startswith("lambdas."))
    x, y = hero.center[0], hero.box[1] + hero.box[3] + obstacle.box[3]/2 + 2
    obstacle.translate(x-obstacle.center[0], y-obstacle.center[1])
    candidate, failure = _candidate(d, hero.node_id, "y", 11.0)
    assert candidate is None and failure


@pytest.mark.private
def test_unknown_child_prevents_singleton_fallback(tmp_path):
    import base64
    import xml.etree.ElementTree as ET
    source = ROOT / "experiments/008-explicit-alignment/runs/002/white/pn_mandate_detailed/after.svg"
    diagram = Diagram.load(source)
    hero = next(n for n in diagram.nodes.values() if n.node_id.startswith("ecs."))
    group = ET.SubElement(diagram.tree.getroot(), "{http://www.w3.org/2000/svg}g",
                          {"class": base64.b64encode(b"ecs.unknown").decode()})
    shape = ET.SubElement(group, "{http://www.w3.org/2000/svg}g", {"class": "shape"})
    ET.SubElement(shape, "{http://www.w3.org/2000/svg}circle", cx="10", cy="10", r="5")
    modified = tmp_path / "source.svg"
    diagram.tree.write(modified)
    output = tmp_path / "output.svg"
    report = center_by_alternatives(modified, output)
    assert not report["applied"]
    assert any("unsupported child" in item["reason"] for item in report["skipped"])
    assert Diagram.load(output).nodes[hero.node_id].center == hero.center
    assert output.read_bytes() == modified.read_bytes()
