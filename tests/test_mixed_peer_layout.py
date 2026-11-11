"""Internal cross-type links must not scatter otherwise independent peers."""
import json
from pathlib import Path

import pytest

from pndocgen.engine.core.models import Component, Edge, InventoryNode
from pndocgen.engine.renderers.d2_generator import D2Generator
from pndocgen.engine.renderers.svg_geometry import Diagram, collisions
from pndocgen.engine.renderers.svg_containers import text_audit, text_conflicts

TEMPLATES = Path(__file__).resolve().parents[1] / "pndocgen/engine/d2/templates"


def sample():
    component = Component("example", "test")
    for cluster, name, kind in [
        ("ecs", "worker", "ecs"), ("apigw", "api", "apigw"),
        ("rules", "rule", "eventbridge_rule"), ("queues", "adapter", "eventbridge_pipe"),
    ]:
        component.add_node(cluster, InventoryNode(name, name, kind, "test", "local"))
    for i in range(11):
        name = f"q{i:02}"
        component.add_node("queues", InventoryNode(name, name, "sqs", "test", "local"))
    edges = [Edge("api", "worker", "apigw_integration", "integrates"),
             Edge("rule", "q00", "eventbridge_trigger", "triggers"),
             Edge("q09", "adapter", "pipe_flow", "forwards"),
             Edge("worker", "q04", "sqs_producer", "produces")]
    edges += [Edge(f"q{i:02}", "worker", "sqs_consumer", "consumes")
              for i in (0, 2, 3, 5, 6, 7, 8, 9)]
    return component, edges


@pytest.mark.parametrize("pattern", ["ecs_microservice", "lambda_microservice"])
def test_rendered_peers_aligned_with_internal_link(tmp_path, pattern):
    component, edges = sample()
    original = component.to_dict()
    outputs = []
    for repeat in range(2):
        gen = D2Generator(TEMPLATES)
        d2 = gen.generate_l3(component, tmp_path / f"run{repeat}.d2",
                             edges if repeat == 0 else list(reversed(edges)),
                             detail_level="detailed", pattern=pattern)
        svg = gen.render(d2)
        diagram = Diagram.load(svg)
        peers = [n for key, n in diagram.nodes.items() if key.rsplit(".", 1)[-1].startswith("q")]
        assert len(peers) == 11
        assert max(n.center[0] for n in peers) - min(n.center[0] for n in peers) < 0.01
        assert len(diagram.nodes) == 15
        assert len(diagram.edges) == len(edges)
        assert not collisions(diagram)
        assert not text_audit(diagram)[0]["overflow"]
        assert not text_conflicts(diagram)
        assert not gen.last_layout_report["alignment_audit"]["violations"]
        ledger = json.loads(d2.with_suffix(".view.json").read_text())
        pairs = {(e.source, e.target) for e in diagram.edges}
        def scoped(p):
            return "ingress." + p if "ingress" in diagram.containers and p.split(".")[0] in {"apigw", "rules", "queues"} else p
        assert {(scoped(a), scoped(b)) for r in ledger["relationships"]
                for a, b in [r["d2_endpoints"]]} == pairs
        assert component.to_dict() == original
        outputs.append((d2.read_bytes(), svg.read_bytes(), ledger))
    assert outputs[0] == outputs[1]


def test_abstention_is_not_successful_alignment(tmp_path):
    from test_explicit_alignment import fixture
    from pndocgen.engine.renderers.svg_alignment import Rule, normalize, audit_alignment
    source = fixture(tmp_path, [("peers.a", 100, 100, "image"),
                               ("peers.b", 450, 230, "image")])
    output = tmp_path / "output.svg"
    rules = {"peers": Rule("column")}
    report = normalize(source, output, rules)
    assert report["skipped"][0]["reason"] == "requested move exceeds max_shift"
    assert output.read_bytes() == source.read_bytes()
    audit = audit_alignment(Diagram.load(output), rules)
    assert audit["violations"] == [{"cluster": "peers", "layout": "column",
                                   "reason": "alignment contract not satisfied", "spread": 350.0}]


@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("layout", ["column", "row", "grid"])
def test_opaque_types_and_unaffected_layouts(internal, layout):
    from copy import deepcopy
    from pndocgen.engine.renderers.peer_layout import group_mixed_peers
    clusters = {"arbitrary": [{"id": "a", "resource_type": "opaque-blue"},
                               {"id": "b", "resource_type": "opaque-blue"},
                               {"id": "c", "resource_type": "opaque-green"}]}
    edges = [{"from_path": "arbitrary.a", "to_path": "arbitrary.c"}] if internal else []
    contracts = {"arbitrary": {"layout": layout, "columns": 2 if layout == "grid" else 1}}
    text = 'arbitrary: "Label" {\n  class: boundary\n' + "".join(
        f'  {name}: "{name}" {{\n    shape: image\n  }}\n' for name in ("a", "b", "c")) + "}\n"
    text += "arbitrary.a -> arbitrary.c: \"flow\"\n" if internal else ""
    original = deepcopy((clusters, edges, contracts))
    result, rules, paths = group_mixed_peers(text, clusters, edges, contracts)
    assert (clusters, edges, contracts) == original
    if internal and layout == "column":
        assert paths == {"arbitrary.a": "arbitrary._layout_peers_0.a",
                         "arbitrary.b": "arbitrary._layout_peers_0.b"}
        assert rules == {"arbitrary._layout_peers_0": {"layout": "column", "columns": 1}}
        assert 'arbitrary._layout_peers_0.a -> arbitrary.c: "flow"' in result
        assert 'style.stroke-width: 0' in result
    else:
        assert (result, rules, paths) == (text, contracts, {})


def test_do_not_group_connected_same_type_peers():
    from pndocgen.engine.renderers.peer_layout import group_mixed_peers
    clusters = {"flow": [{"id": name, "resource_type": kind}
                        for name, kind in (("a", "blue"), ("b", "blue"), ("c", "green"))]}
    edges = [{"from_path": "flow.a", "to_path": "flow.b"},
             {"from_path": "flow.b", "to_path": "flow.c"}]
    contracts = {"flow": {"layout": "column", "columns": 1}}
    assert group_mixed_peers("unchanged", clusters, edges, contracts) == ("unchanged", contracts, {})


def test_scope_names_do_not_shadow_real_resources():
    from pndocgen.engine.renderers.peer_layout import group_mixed_peers
    clusters = {"flow": [{"id": name, "resource_type": kind} for name, kind in
                        (("a", "blue"), ("b", "blue"), ("_layout_peers_0", "green"))]}
    edges = [{"from_path": "flow.a", "to_path": "flow._layout_peers_0"}]
    contracts = {"flow": {"layout": "column", "columns": 1}}
    text = 'flow: "Flow" {\n' + "".join(
        f'  {n["id"]}: "Label" {{\n    shape: image\n  }}\n' for n in clusters["flow"]) + "}\n"
    text += "flow.a -> flow._layout_peers_0: \"flow\"\n"
    result, rules, paths = group_mixed_peers(text, clusters, edges, contracts)
    assert set(rules) == {"flow._layout_peers_0_"}
    assert paths["flow.a"] == "flow._layout_peers_0_.a"
    assert 'flow._layout_peers_0_.a -> flow._layout_peers_0: "flow"' in result


@pytest.mark.parametrize('connected', [False, True])
def test_distinct_singletons_only_relax_column_when_they_form_a_flow(connected):
    from pndocgen.engine.renderers.peer_layout import group_mixed_peers
    clusters = {'flow': [{'id': 'a', 'resource_type': 'blue'},
                         {'id': 'b', 'resource_type': 'green'}]}
    edges = [{'from_path': 'flow.a', 'to_path': 'flow.b'}] if connected else []
    contracts = {'flow': {'layout': 'column', 'columns': 1}}
    text, rules, paths = group_mixed_peers('unchanged', clusters, edges, contracts)
    assert text == 'unchanged' and paths == {}
    assert rules == {'flow': {'layout': 'free' if connected else 'column', 'columns': 1}}
    assert contracts['flow']['layout'] == 'column'
