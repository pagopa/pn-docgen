from pndocgen.engine.core.models import Component, InventoryNode, Edge
from pndocgen.engine.core.view_config import ViewConfig
from pndocgen.engine.core.config import EdgeFilterConfig
from pndocgen.engine.renderers.view import prepare_view
from pndocgen.engine.renderers.d2_generator import D2Generator

def test_table_bucket_projection_preserves_inventory_and_distinct_buckets():
    c = Component('service', 'local')
    for name, kind in [('a','aws_s3tables_table'),('b','aws_s3tables_table'),
                       ('bucket1','aws_s3tables_tablebucket'),('bucket2','aws_s3tables_tablebucket'),('writer','lambda')]:
        c.add_node('storage', InventoryNode(name,name,kind,'local','local'))
    edges = [Edge('a','bucket1','table_storage'), Edge('b','bucket2','table_storage'), Edge('writer','a','writes')]
    before = c.to_dict()
    view, selected, report = prepare_view(c,edges,ViewConfig(),EdgeFilterConfig(),D2Generator._safe_id)
    assert {n.name for cl in view.clusters.values() for n in cl.nodes} == {'bucket1','bucket2','writer'}
    assert [(e.source,e.target) for e in selected] == [('writer','bucket1')]
    assert c.to_dict() == before and edges[-1].target == 'a'
    assert len(report['relationships']) == 3

def test_missing_owner_does_not_invent_bucket():
    c = Component('service','local')
    c.add_node('storage',InventoryNode('a','a','aws_s3tables_table','local','local'))
    _,selected,report = prepare_view(c,[Edge('external','a','writes')],ViewConfig(),EdgeFilterConfig(),D2Generator._safe_id)
    assert not selected
    assert report['resources'][0]['status'] == 'unresolved_table_bucket'
