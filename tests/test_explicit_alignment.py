"""Focused contracts and guard tests for the isolated experiment."""
import base64
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

from pndocgen.engine.renderers.svg_alignment import Rule, normalize, targets
from pndocgen.engine.renderers.svg_layout import read_contracts, CONTRACT_PREFIX
import json
from pndocgen.engine.renderers.svg_geometry import Diagram

NS = "{http://www.w3.org/2000/svg}"


def fixture(tmp_path, nodes, edges=(), *, transform=False, reverse=False):
    """nodes: (id, center_x, center_y, shape), independent of resource categories."""
    root = ET.Element(NS + "svg", viewBox="0 0 900 700")
    if transform:
        root.set("transform", "translate(1 1)")

    def group(identifier, classes=""):
        encoded = base64.b64encode(identifier.encode()).decode()
        return ET.SubElement(root, NS + "g", {"class": encoded + " " + classes})

    clusters = sorted({name.rsplit(".", 1)[0] for name, *_ in nodes})
    for cluster in clusters:
        shape = ET.SubElement(group(cluster, "boundary"), NS + "g", {"class": "shape"})
        ET.SubElement(shape, NS + "rect", x="0", y="0", width="850", height="650")
    for name, x, y, shape_type in reversed(nodes) if reverse else nodes:
        shape = ET.SubElement(group(name), NS + "g", {"class": "shape"})
        ET.SubElement(shape, NS + shape_type, x=str(x-10), y=str(y-10), width="20", height="20")
    for i, (source, target, path) in enumerate(edges):
        ET.SubElement(group(f"({source} -> {target})[{i}]"), NS + "path", d=path)
    source = tmp_path / "source.svg"
    ET.ElementTree(root).write(source)
    return source


@pytest.mark.parametrize("shape", ["image", "rect"])
@pytest.mark.parametrize("layout", ["column", "row"])
def test_two_nodes_align_without_majority(tmp_path, shape, layout):
    nodes = [("arbitrary.a", 100, 100, shape), ("arbitrary.b", 118, 230, shape)]
    if layout == "row":
        nodes = [(name, y, x, kind) for name, x, y, kind in nodes]
    source = fixture(tmp_path, nodes)
    original = source.read_bytes()
    target = tmp_path / "after.svg"
    report = normalize(source, target, {"arbitrary": Rule(layout)})
    d = Diagram.load(target)
    axis = 0 if layout == "column" else 1
    assert {n.center[axis] for n in d.nodes.values()} == {109.0}
    assert len(report["applied"][0]["moves"]) == 2
    assert source.read_bytes() == original
    normalize(target, tmp_path / "again.svg", {"arbitrary": Rule(layout)})
    assert (tmp_path / "again.svg").read_bytes() == target.read_bytes()


def test_column_with_no_majority_and_mixed_shapes(tmp_path):
    source = fixture(tmp_path, [("mixed.a", 80, 100, "image"), ("mixed.b", 95, 200, "rect"),
                                ("mixed.c", 112, 300, "rect")])
    normalize(source, tmp_path / "out.svg", {"mixed": Rule("column")})
    assert {n.center[0] for n in Diagram.load(tmp_path / "out.svg").nodes.values()} == {95.0}


def test_ragged_grid_aligns_rows_without_reordering(tmp_path):
    nodes = []
    for col, ys in enumerate(([100, 200, 300, 400], [119, 259, 379], [119, 259, 379])):
        nodes += [(f"grid.c{col}r{row}", 100 + col*160, y, "rect") for row, y in enumerate(ys)]
    source = fixture(tmp_path, nodes)
    normalize(source, tmp_path / "out.svg", {"grid": Rule("grid", columns=3)})
    d = Diagram.load(tmp_path / "out.svg")
    for col in range(3):
        for row in range(4 if col == 0 else 3):
            assert d.nodes[f"grid.c{col}r{row}"].center == (100+col*160, 100+row*100)


@pytest.mark.parametrize("reason,rule", [
    ("ambiguous", Rule("grid", columns=3)),
    ("max_shift", Rule("column", max_shift=2)),
    ("no explicit", Rule("free")),
])
def test_refusal_is_reported_and_source_preserved(tmp_path, reason, rule):
    source = fixture(tmp_path, [("group.a", 100, 100, "rect"), ("group.b", 120, 200, "rect")])
    target = tmp_path / "out.svg"
    report = normalize(source, target, {"group": rule})
    assert reason in report["skipped"][0]["reason"]
    assert target.read_bytes() == source.read_bytes()


def test_collision_rolls_back_whole_cluster(tmp_path):
    source = fixture(tmp_path, [("group.a", 80, 100, "rect"), ("group.b", 140, 200, "rect"),
                                ("obstacle.a", 110, 100, "rect")])
    target = tmp_path / "out.svg"
    report = normalize(source, target, {"group": Rule("column")})
    assert any("collision" in item["reason"] for item in report["skipped"])
    assert target.read_bytes() == source.read_bytes()


def test_parallel_endpoint_move_preserves_other_endpoint(tmp_path):
    source = fixture(tmp_path, [("group.a", 100, 100, "rect"), ("group.b", 118, 230, "rect"),
                                ("other.a", 400, 100, "rect")],
                     [("group.a", "other.a", "M 112 100 L 388 100")])
    target = tmp_path / "out.svg"
    report = normalize(source, target, {"group": Rule("column")})
    d = Diagram.load(target)
    assert report["applied"]
    assert d.edges[0].start == (121, 100)
    assert d.edges[0].end == (388, 100)


def test_perpendicular_move_with_elbow(tmp_path):
    source = fixture(tmp_path, [("group.a", 100, 100, "rect"), ("group.b", 118, 300, "rect"),
                                ("other.a", 400, 200, "rect")],
                     [("group.a", "other.a", "M 100 112 L 100 160 S 100 170 110 170 L 360 170 S 370 170 370 180 L 370 200 L 388 200")])
    target = tmp_path / "out.svg"
    report = normalize(source, target, {"group": Rule("column")})
    d = Diagram.load(target)
    assert report["applied"]
    assert d.edges[0].start == (109, 112)
    assert d.edges[0].end == (388, 200)


@pytest.mark.parametrize("path,needle", [
    ("M 100 112 L 100 388", "senza pieghe"),
    ("M 100 112 Q 105 200 100 388", "unsupported"),
])
def test_unmovable_incident_edge_keeps_entire_pair(tmp_path, path, needle):
    source = fixture(tmp_path, [("group.a", 100, 100, "rect"), ("group.b", 118, 230, "rect"),
                                ("other.a", 100, 400, "rect")], [("group.a", "other.a", path)])
    target = tmp_path / "out.svg"
    report = normalize(source, target, {"group": Rule("column")})
    assert needle in report["skipped"][0]["reason"]
    assert target.read_bytes() == source.read_bytes()


def test_no_overwrite_and_transform_refusal(tmp_path):
    source = fixture(tmp_path, [("group.a", 100, 100, "rect"), ("group.b", 118, 230, "rect")], transform=True)
    target = tmp_path / "out.svg"
    report = normalize(source, target, {"group": Rule("column")})
    assert "transform" in report["skipped"][0]["reason"]
    assert target.read_bytes() == source.read_bytes()
    with pytest.raises(FileExistsError):
        normalize(source, target, {})
    with pytest.raises(FileExistsError):
        normalize(source, source, {})


def test_rule_contract_comes_from_generator_metadata(tmp_path):
    source = fixture(tmp_path, [("custom.a", 100, 100, "rect"), ("custom.b", 118, 230, "rect")])
    d2 = tmp_path / "diagram.d2"
    d2.write_text(CONTRACT_PREFIX + json.dumps({"version": 1, "clusters": {"custom": {"layout": "column", "columns": 1}}}))
    assert Rule(**read_contracts(d2)["custom"]) == Rule("column")


@pytest.mark.parametrize("kwargs", [{"layout": "unknown"}, {"layout": "grid", "columns": 0},
                                    {"layout": "row", "max_shift": float("nan")}])
def test_invalid_contract(kwargs):
    with pytest.raises(ValueError):
        Rule(**kwargs)


def test_xml_node_order_does_not_change_targets(tmp_path):
    nodes = [("custom.a", 100, 100, "rect"), ("custom.b", 118, 230, "rect")]
    source = fixture(tmp_path, nodes)
    expected = targets(Diagram.load(source), "custom", Rule("column"))
    source = fixture(tmp_path, nodes, reverse=True)
    assert targets(Diagram.load(source), "custom", Rule("column")) == expected


def test_unsupported_shape_is_explicitly_reported(tmp_path):
    source = fixture(tmp_path, [("custom.a", 100, 100, "ellipse"), ("custom.b", 118, 230, "rect")])
    target = tmp_path / "out.svg"
    report = normalize(source, target, {"custom": Rule("column")})
    assert "unsupported child geometry" in report["skipped"][0]["reason"]
    assert target.read_bytes() == source.read_bytes()
