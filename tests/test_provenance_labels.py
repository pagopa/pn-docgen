"""Deterministic CFN labels: synthetic membership, no AWS and no name heuristics."""
import copy
import itertools
import json
from pathlib import Path
import random
import sys

import pytest

from pndocgen.engine.core.models import InventoryNode
from pndocgen.engine.renderers.node_labels import choose_labels
from pndocgen.sources.aws.cfn_provenance import logical_paths
from test_lambda_fidelity import discoverer


def record(kind, physical, logical, stack):
    return {'type': kind, 'physical_id': physical, 'logical_id': logical, 'stack_name': stack}


def hierarchy():
    return [record('AWS::CloudFormation::Stack', 'child-a', 'WorkerMicroservice', 'root'),
            record('AWS::CloudFormation::Stack', 'child-b', 'RemediationMicroservice', 'root'),
            record('AWS::ECS::Service', 'arn:aws:ecs:eu-west-1:000000000000:service/cluster/long-dev-ECSService-a', 'ECSService', 'child-a'),
            record('AWS::ECS::Service', 'arn:aws:ecs:eu-west-1:000000000000:service/cluster/long-dev-ECSService-b', 'ECSService', 'child-b')]


def node(name, path=None, kind='AWS::ECS::Service'):
    metadata = {'cfn_type': kind}
    if path is not None:
        metadata.update(cfn_provenance='stack_membership_v1', cfn_logical_path=path, logical_id=path[-1])
    return InventoryNode('test://' + name, name, 'ecs_service', 'synthetic', 'local', metadata=metadata)


def labels(nodes, prefixes=None):
    return choose_labels(nodes, {n.name: n.name for n in nodes}, prefixes)[0]


def test_all_raw_orders_have_identical_provenance():
    expected, _ = logical_paths(hierarchy())
    for records in itertools.permutations(hierarchy()):
        paths, issues = logical_paths(list(records))
        assert paths == expected and not issues


@pytest.mark.parametrize('child_first', [False, True])
@pytest.mark.parametrize('reverse_pages', [False, True])
def test_collector_preserves_membership_when_child_is_seen_first(child_first, reverse_pages):
    rows = hierarchy()
    def response(params):
        items = [r for r in rows if r['stack_name'] == params['StackName']]
        if reverse_pages:
            items.reverse()
        index = int(params.get('NextToken', '0'))
        selected = items[index:index+1]
        result = {'StackResourceSummaries': [{'ResourceType': r['type'], 'PhysicalResourceId': r['physical_id'],
                   'LogicalResourceId': r['logical_id']} for r in selected]}
        if index + 1 < len(items):
            result['NextToken'] = str(index + 1)
        return result
    d, session = discoverer({'cloudformation': {'list_stack_resources': response}}, [])
    d.component_resources = {}
    if child_first:
        d._process_stack('child-a', 'example', 0)
    d._process_stack('root', 'example', 0)
    nodes = [n for n in d.to_inventory_nodes() if n.resource_type == 'ecs_service']
    assert set(labels(nodes).values()) == {'Worker Microservice', 'Remediation Microservice'}
    assert len([c for c in session.calls if c[2]['StackName'] == 'child-a']) == 1
    assert not session.unexpected and not d.issues


@pytest.mark.parametrize('mode', ['multiple_parents', 'cycle', 'resource_owners', 'logical_slot'])
def test_ambiguous_provenance_abstains(mode):
    rows = hierarchy()
    if mode == 'multiple_parents':
        rows.append(record('AWS::CloudFormation::Stack', 'child-a', 'OtherParent', 'other'))
    elif mode == 'cycle':
        rows.append(record('AWS::CloudFormation::Stack', 'root', 'Cycle', 'child-a'))
    elif mode == 'resource_owners':
        rows.append({**rows[2], 'stack_name': 'child-b'})
    else:
        rows.append({**rows[2], 'physical_id': 'different-physical-resource'})
    paths, issues = logical_paths(rows)
    assert (rows[2]['type'], rows[2]['physical_id']) not in paths
    assert issues
    assert logical_paths(list(reversed(rows))) == (paths, issues)


def test_legacy_capture_does_not_gain_invented_provenance():
    rows = [{k: v for k, v in r.items() if k != 'stack_name'} for r in hierarchy()]
    paths, issues = logical_paths(rows)
    assert not paths and not issues
    d, _ = discoverer(resources=rows)
    nodes = d.to_inventory_nodes()
    assert all('cfn_logical_path' not in n.metadata for n in nodes)
    assert labels(nodes) == {n.name: n.name for n in nodes}


def test_formatted_collision_expands_all_members_simultaneously():
    nodes = [node('a', ['LeftSide', 'ABCWorker']), node('b', ['RightSide', 'AbcWorker'])]
    expected = {'a': 'Left Side / ABC Worker', 'b': 'Right Side / Abc Worker'}
    assert labels(nodes) == labels(list(reversed(nodes))) == expected


def test_logical_label_must_not_collide_with_legacy_label():
    nodes = [node('Worker'), node('physical', ['Parent', 'Worker'])]
    assert labels(nodes) == {'Worker': 'Worker', 'physical': 'Parent / Worker'}


def test_prefixes_participate_in_final_label_collision_check():
    nodes = [node('S3 Tables · Worker'), node('physical', ['Parent', 'Worker'])]
    result = labels(nodes, {'physical': 'S3 Tables · '})
    assert result['physical'] == 'S3 Tables · Parent / Worker'


def test_identical_paths_fall_back_without_numbering():
    nodes = [node('physical-a', ['Parent', 'Worker']), node('physical-b', ['Parent', 'Worker'])]
    assert labels(nodes) == {'physical-a': 'physical-a', 'physical-b': 'physical-b'}


@pytest.mark.parametrize('kind,leaf', [('AWS::ECS::Service', 'ECSService'),
                                     ('AWS::Lambda::Function', 'Function'),
                                     ('AWS::SQS::Queue', 'SQSQueue'),
                                     ('AWS::DynamoDB::Table', 'Table')])
def test_generic_leaf_rule_is_type_based(kind, leaf):
    assert labels([node('physical', ['BillingWorker', leaf], kind)]) == {'physical': 'Billing Worker'}
    assert labels([node('physical', [leaf], kind)]) == {'physical': 'physical'}


def test_physical_replacement_does_not_change_verified_label():
    assert set(labels([node('old-random', ['BillingWorker', 'ECSService'])]).values()) == set(
        labels([node('new-random', ['BillingWorker', 'ECSService'])]).values())


def test_labels_do_not_mutate_identity_or_metadata():
    nodes = [node('a', ['WorkerA', 'ECSService']), node('b', ['WorkerB', 'ECSService'])]
    before = copy.deepcopy(nodes)
    labels(nodes)
    assert nodes == before


def test_shuffle_many_resources_is_repeatable():
    nodes = [node(f'physical-{i}', [f'Group{i}', 'Worker', 'ECSService']) for i in range(30)]
    expected = labels(nodes)
    for seed in range(10):
        shuffled = list(nodes)
        random.Random(seed).shuffle(shuffled)
        assert labels(shuffled) == expected
    assert len({v.casefold() for v in expected.values()}) == len(nodes)


@pytest.mark.parametrize('path', [['not-a-logical-id'], [''], [123]])
def test_malformed_provenance_keeps_fallback(path):
    assert labels([node('physical', path)]) == {'physical': 'physical'}


@pytest.mark.parametrize('detail', ['simplified', 'detailed'])
def test_cli_roundtrip_preserves_labels_but_not_arbitrary_metadata(detail, tmp_path, monkeypatch):
    from pndocgen.cli import main
    config = Path(__file__).resolve().parents[1] / 'pndocgen.yaml'
    nodes = [node('example-long-a', ['WorkerMicroservice', 'ECSService']),
             node('example-long-b', ['RemediationMicroservice', 'ECSService'])]
    resources = [{'id': n.id, 'name': n.name, 'type': n.resource_type,
                  'account': n.account, 'region': n.region,
                  'tags': {'Microservice': 'example'},
                  'metadata': {**n.metadata, 'component': 'example',
                               'private_unrelated_field': 'DO_NOT_COPY'}} for n in nodes]
    graphs, svgs = [], []
    for tag, rows in [('original', resources), ('inverted', list(reversed(resources)))]:
        source, graph, target = tmp_path / (tag + '.json'), tmp_path / (tag + '_graph.json'), tmp_path / tag
        source.write_text(json.dumps({'account': 'synthetic', 'region': 'local', 'resources': rows, 'relationships': []}))
        monkeypatch.setattr(sys, 'argv', ['pn-docgen', 'resolve', '--inventory', str(source),
                            '--config', str(config), '--output', str(graph)])
        main()
        assert 'cfn_logical_path' in graph.read_text()
        assert 'DO_NOT_COPY' not in graph.read_text()
        monkeypatch.setattr(sys, 'argv', ['pn-docgen', 'generate', '--graph', str(graph),
                            '--config', str(config), '--output-dir', str(target), '--level', 'l3',
                            '--detail-level', detail, '--render'])
        main()
        svg, = target.glob('*.svg')
        assert 'Worker Microservice' in svg.read_text() and 'Remediation Microservice' in svg.read_text()
        ledger = json.loads(svg.with_suffix('.view.json').read_text())
        assert len(ledger['display_labels']) == 2
        graphs.append(graph.read_bytes())
        svgs.append(svg.read_bytes())
    assert graphs[0] == graphs[1] and svgs[0] == svgs[1]
