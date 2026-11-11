from pathlib import Path
import pytest
from pndocgen.engine.core.models import Component, InventoryNode
from pndocgen.engine.renderers import d2_generator as module


@pytest.mark.parametrize("pattern,detail", [("lambda_microservice", "detailed"),
    ("ecs_microservice", "detailed"), ("ecs_microservice", "simplified")])
def test_missing_icon_renders_rectangle(tmp_path, monkeypatch, pattern, detail):
    monkeypatch.setattr(module, "_ICON_URL", {})
    component = Component("example", "local")
    component.add_node("apigw", InventoryNode("fake://api", "api", "apigw", "local", "local"))
    generator = module.D2Generator(Path(module.__file__).parents[1] / "d2/templates")
    d2 = generator.generate_l3(component, tmp_path / "test.d2", pattern=pattern, detail_level=detail)
    assert "shape: rectangle" in d2.read_text()
    assert generator.render(d2).is_file()
