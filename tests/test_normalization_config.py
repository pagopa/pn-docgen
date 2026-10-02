import json
import pytest

from pndocgen.engine.core.config import AppConfig
from pndocgen.engine.core.normalization_config import NormalizationConfig
from pndocgen.engine.renderers.svg_layout import CONTRACT_PREFIX, read_contracts


@pytest.mark.parametrize("raw", [{"max_shift": -1}, {"max_shift": True},
    {"min_gap": "24"}, {"min_gap": float("inf")}, {"enabled": "false"},
    {"version": True}, {"version": 2}, {"min_terminal": 9}, {"typo": 4}, []])
def test_invalid_settings_fail_explicitly(raw):
    with pytest.raises(ValueError):
        NormalizationConfig.from_mapping(raw)


def test_yaml_loads_settings(tmp_path):
    source = tmp_path / "config.yaml"
    source.write_text("render:\n  normalization:\n    enabled: false\n    max_shift: 80\n    max_node_shift: 12\n")
    result = AppConfig.from_yaml(source).render.normalization
    assert not result.enabled and result.max_shift == 80 and result.max_node_shift == 12


def test_legacy_d2_does_not_infer_layout(tmp_path):
    source = tmp_path / "legacy.d2"
    source.write_text('group: "Group" {\n direction: down\n}\n')
    assert read_contracts(source) is None


@pytest.mark.parametrize("record", [
    {"version": 2, "clusters": {}}, {"version": 1, "clusters": {"a": {"layout": "grid", "columns": 0}}},
    {"version": 1, "clusters": {"a": {"layout": "unknown", "columns": 1}}}])
def test_invalid_metadata_fails(tmp_path, record):
    source = tmp_path / "bad.d2"
    source.write_text(CONTRACT_PREFIX + json.dumps(record))
    with pytest.raises(ValueError):
        read_contracts(source)
