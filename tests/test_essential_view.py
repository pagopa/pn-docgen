from copy import deepcopy
from pathlib import Path
import pytest
from pndocgen.engine.core.models import Component, InventoryNode, Edge
from pndocgen.engine.renderers.d2_generator import D2Generator
from pndocgen.engine.core import config
from pndocgen.engine.core.view_config import ViewConfig


def test_cluster_exclusion_defaults():
    assert ViewConfig().excluded_clusters == ["security", "misc"]
    assert ViewConfig.from_mapping(None).excluded_clusters == ["security", "misc"]
    assert ViewConfig.from_mapping({}).excluded_clusters == ["security", "misc"]
    assert ViewConfig.from_mapping({"excluded_clusters": []}).excluded_clusters == []


@pytest.mark.parametrize("value", [None, "security", [""], [" misc"], [1]])
def test_invalid_cluster_exclusions(value):
    with pytest.raises(ValueError, match="excluded_clusters"):
        ViewConfig.from_mapping({"excluded_clusters": value})


@pytest.mark.parametrize("pattern", ["ecs_microservice", "lambda_microservice"])
@pytest.mark.parametrize("detail", ["simplified", "detailed"])
@pytest.mark.parametrize("excluded", [[], ["security", "misc", "rules"]])
def test_configurable_cluster_visibility(pattern, detail, excluded, tmp_path, monkeypatch):
    settings = config.AppConfig()
    settings.render.view = ViewConfig.from_mapping({"excluded_clusters": excluded})
    monkeypatch.setattr(config, "_cached_config", settings)
    component = Component("example", "local")
    for cluster, name, kind in [("misc", "unknown", "future_type"),
                                ("security", "auth", "aws_apigateway_authorizer"),
                                ("rules", "timer", "aws_scheduler_schedule"),
                                ("lambdas", "worker", "lambda")]:
        component.add_node(cluster, InventoryNode("test://" + name, name, kind, "local", "local"))
    original = deepcopy(component)
    generator = D2Generator(Path(__file__).parents[1] / "pndocgen/engine/d2/templates")
    diagram = generator.generate_l3(component, tmp_path / "view.d2",
                                    pattern=pattern, detail_level=detail)
    included = {r["name"] for r in generator.last_view_report["resources"]
                if r["status"] == "included"}
    assert included == ({"worker"} if excluded else {"unknown", "auth", "timer", "worker"})
    assert component == original
    assert generator.render(diagram).exists()


@pytest.mark.parametrize('pattern', ['ecs_microservice', 'lambda_microservice'])
@pytest.mark.parametrize('detail', ['simplified', 'detailed'])
def test_omit_auxiliary_keep_primary_and_report(pattern, detail, tmp_path):
    c = Component('example', 'local')
    for cluster, name, kind in [('misc','unknown','new_type'),
        ('misc','auth','aws_apigateway_authorizer'), ('misc','timer','aws_scheduler_schedule'),
        ('lambdas','worker','lambda'), ('dynamodb','table','dynamodb')]:
        c.add_node(cluster,InventoryNode('test://'+name,name,kind,'local','local'))
    edges = [Edge('auth','worker','authorization'), Edge('worker','table','storage_access')]
    before = deepcopy((c,edges))
    g = D2Generator(Path(__file__).parents[1]/'pndocgen/engine/d2/templates')
    d2 = g.generate_l3(c,tmp_path/'result.d2',edges,detail_level=detail,pattern=pattern)
    report = g.last_view_report
    assert {r['name'] for r in report['resources'] if r['status']=='excluded_auxiliary_cluster'} == {'unknown','auth'}
    assert next(r for r in report['resources'] if r['name']=='timer')['status']=='included'
    assert next(r for r in report['relationships'] if r['type']=='authorization')['status']=='excluded_endpoint'
    assert 'misc:' not in d2.read_text() and 'security:' not in d2.read_text()
    assert (c,edges)==before
    assert g.render(d2).exists()
