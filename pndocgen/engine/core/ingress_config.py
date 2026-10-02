"""Common ingress-view default, optional central exceptions, explicit CLI override."""
from dataclasses import dataclass, field


@dataclass
class IngressConfig:
    mode: str = "peers"
    components: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.mode not in ("peers", "legacy") or not isinstance(self.components, dict):
            raise ValueError("render.ingress requires mode peers|legacy and a components mapping")
        if any(not isinstance(k, str) or not k or v not in ("peers", "legacy") for k,v in self.components.items()):
            raise ValueError("Ingress component overrides must be peers|legacy")

    @classmethod
    def from_mapping(cls, value):
        if value is None:
            return cls()
        if not isinstance(value, dict) or set(value) - {"mode", "components"}:
            raise ValueError("Unknown render.ingress settings")
        return cls(**value)

    def resolve(self, component, override=None):
        if override is not None:
            if override not in ("peers", "legacy"):
                raise ValueError("Ingress CLI override must be peers|legacy")
            return override, "CLI"
        if component in self.components:
            return self.components[component], "component override"
        return self.mode, "common default"
