from copy import deepcopy

import pytest

from pndocgen.engine.core.config import EdgeFilterConfig
from pndocgen.engine.core.models import Component, Edge, InventoryNode
from pndocgen.engine.core.view_config import ViewConfig
from pndocgen.engine.renderers.view import prepare_view


def run(rules, name="example", global_filter=None):
    component = Component(name, "local")
    for node in ("a", "b", "c"):
        component.add_node("lambdas", InventoryNode("test://" + node, node, "lambda", "local", "local"))
    edges = [Edge("a", "b", "dynamodb_stream"), Edge("a", "c", "dynamodb_stream")]
    before = deepcopy((component, edges))
    result = prepare_view(component, edges, ViewConfig.from_mapping({"edge_exclusions": rules}),
                          global_filter or EdgeFilterConfig(), lambda name: name)
    assert (component, edges) == before
    return result


@pytest.mark.parametrize("selectors,remaining", [({}, 0), ({"source": "a"}, 0),
    ({"target": "b"}, 1), ({"source": "a", "target": "c"}, 1),
    ({"source": "A"}, 2), ({"source": "a*"}, 2)])
def test_exact_selectors(selectors, remaining):
    rule = {"component": "example", "edge_type": "dynamodb_stream", **selectors}
    view, edges, report = run([rule])
    assert len(edges) == remaining
    assert sum(len(cluster.nodes) for cluster in view.clusters.values()) == 3
    for relation in report["relationships"]:
        if relation["status"] == "excluded_edge_rule":
            assert relation["matched_rule"] == rule
    assert len(run([rule], name="other")[1]) == 2


def test_global_filter_precedence():
    rule = {"component": "example", "edge_type": "dynamodb_stream"}
    _, edges, report = run([rule], global_filter=EdgeFilterConfig(exclude=["dynamodb_stream"]))
    assert edges == []
    assert all(r["status"] == "excluded_edge_type" for r in report["relationships"])


def test_canonical_rules():
    a = {"component": "example", "edge_type": "dynamodb_stream"}
    b = {**a, "target": "b"}
    assert ViewConfig.from_mapping({"edge_exclusions": [a, b, a]}) == ViewConfig.from_mapping({"edge_exclusions": [b, a]})
    assert run([])[1]


@pytest.mark.parametrize("rules", [None, {}, [None], [{}],
    [{"component": "x", "edge_type": ""}],
    [{"component": "x", "edge_type": "type", "source": None}],
    [{"component": "x", "edge_type": "type", "target": " a"}],
    [{"component": "x", "edge_type": "type", "typo": "a"}]])
def test_invalid_rules(rules):
    with pytest.raises(ValueError):
        ViewConfig.from_mapping({"edge_exclusions": rules})
