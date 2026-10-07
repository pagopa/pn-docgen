"""Aggregation keeps each resource and accounts for every selected relation."""
import json
from pathlib import Path

import pytest

from pndocgen.engine.core.models import Component,InventoryNode,Edge
from pndocgen.engine.renderers.d2_generator import D2Generator
from pndocgen.engine.renderers.svg_geometry import Diagram
from pndocgen.engine.renderers.svg_containers import text_audit

TEMPLATES = Path(__file__).resolve().parents[1]/'pndocgen/engine/d2/templates'


@pytest.mark.parametrize('intra',[False,True])
@pytest.mark.parametrize('theme',['white','pastel'])
def test_lambda_simplified_grid_and_internal_relations(tmp_path,intra,theme):
    component = Component('example','test')
    component.add_node('apigw',InventoryNode('api','api','apigw','test','local'))
    component.add_node('dynamodb',InventoryNode('table','table','dynamodb','test','local'))
    for i in range(5):
        component.add_node('lambdas',InventoryNode(f'w{i}',f'worker{i}','lambda','test','local'))
    edges=[Edge('api',f'worker{i}','apigw_integration','integrates') for i in range(5)]
    edges += [Edge(f'worker{i}','table','storage_access','reads' if i<3 else 'writes') for i in range(5)]
    edges += [Edge('table','worker0','dynamodb_stream','stream')]
    if intra:
        edges += [Edge('worker0','worker1','http_call','calls'),Edge('worker1','worker0','http_call','responds')]
    outputs=[]
    for repeat in range(2):
        gen=D2Generator(TEMPLATES,theme)
        d2=gen.generate_l3(component,tmp_path/f'run{repeat}.d2',edges if repeat==0 else list(reversed(edges)),detail_level='simplified')
        svg=gen.render(d2)
        diagram=Diagram.load(svg)
        report=json.loads(d2.with_suffix('.view.json').read_text())
        assert len(diagram.nodes)==7
        assert len(diagram.edges)==(5 if intra else 3)
        assert len(report['relationships'])==len(edges)
        assert all('d2_endpoints' in r for r in report['relationships'])
        pairs={(e.source,e.target) for e in diagram.edges}
        assert all(tuple(r['d2_endpoints']) in pairs for r in report['relationships'])
        assert 'reads; writes' in d2.read_text()
        assert not text_audit(diagram)[0]['overflow']
        assert not text_audit(diagram)[0]['unmeasured']
        if intra:
            assert ('lambdas.worker0','lambdas.worker1') in pairs
            assert ('lambdas.worker1','lambdas.worker0') in pairs
        outputs.append((d2.read_bytes(),svg.read_bytes(),report))
    assert outputs[0]==outputs[1]
