"""Current private render contract. Fixtures/manifest stay outside public Git."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pndocgen.services import pipeline
from pndocgen.engine.renderers.svg_geometry import Diagram
from pndocgen.engine.renderers.svg_containers import text_audit

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.private
@pytest.mark.parametrize('name',['delivery','mandate','radd-alt','auth-fleet','paper-channel'])
@pytest.mark.parametrize('detail',['simplified','detailed'])
@pytest.mark.parametrize('theme',['white','pastel'])
def test_current_private_render_contract(name,detail,theme,private_root,request,tmp_path):
    location=request.config.getoption('--private-baselines')
    if location is None:
        pytest.skip('Current private acceptance requires --private-baselines')
    baseline=Path(location)
    manifest=json.loads((baseline/'manifest.json').read_text())
    assert manifest['version']==1
    record=manifest['cases'][f'{name}/{detail}/{theme}']
    digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    source=private_root/record['source_graph']
    expected=baseline/record['svg']
    assert digest(source)==record['source_sha256'], 'Private input changed'
    assert digest(expected)==record['svg_sha256'], 'Acceptance reference changed'
    assert digest(expected.with_suffix('.d2'))==record['d2_sha256']
    assert digest(expected.with_suffix('.view.json'))==record['view_sha256']
    # ROOT may be monkeypatched by legacy fixture support; use the actual
    # package checkout for config, not the private capture root.
    config=Path(__file__).resolve().parents[1]/'pndocgen.yaml'
    output=tmp_path/'current'
    pipeline.generate(SimpleNamespace(graph=str(source),config=str(config),output_dir=str(output),
        level='l3',detail_level=detail,theme=theme,render=True,_run_ts='acceptance'))
    actual=next(output.glob('*.svg'))
    assert digest(actual)==record['svg_sha256']
    assert digest(actual.with_suffix('.d2'))==record['d2_sha256']
    assert digest(actual.with_suffix('.view.json'))==record['view_sha256']
    diagram=Diagram.load(actual)
    audit,_=text_audit(diagram)
    assert not audit['overflow'] and not audit['unmeasured']
    view=json.loads(actual.with_suffix('.view.json').read_text())
    assert len(diagram.nodes)==sum(r['status']=='included' for r in view['resources'])
    assert digest(source)==record['source_sha256']
