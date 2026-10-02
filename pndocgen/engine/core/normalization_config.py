"""Validated configuration for the explicit SVG post-layout passes."""
from dataclasses import dataclass, fields
import math


@dataclass(frozen=True)
class NormalizationConfig:
    version: int = 1
    enabled: bool = True
    prefer_free_nodes: bool = True
    center_singletons: bool = True
    center_grouped_nodes: bool = True
    separate_edge_labels: bool = True
    max_label_shift: float = 120.0
    max_shift: float = 120.0
    min_gap: float = 24.0
    min_terminal: float = 10.0
    max_node_shift: float = 30.0
    fit_text_containers: bool = True
    text_container_margin: float = 16.0
    max_container_growth: float = 240.0
    port_capacity_classes: tuple[str, ...] = ('ecs_hero',)
    max_port_icon_growth: float = 24.0
    port_icon_padding: float = 2.0
    align_node_labels: bool = True
    node_label_gap: float = 4.0
    separate_container_titles: bool = True
    max_title_shift: float = 120.0

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise ValueError("render.normalization.version must be 1")
        for name in ("enabled", "prefer_free_nodes", "center_singletons", "center_grouped_nodes", "separate_edge_labels", "fit_text_containers", "align_node_labels", "separate_container_titles"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"render.normalization.{name} must be boolean")
        for name in ("max_shift", "min_gap", "min_terminal", "max_node_shift", "max_label_shift", "text_container_margin", "max_container_growth", "max_port_icon_growth", "port_icon_padding", "node_label_gap", "max_title_shift"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"render.normalization.{name} must be finite and nonnegative")
        import re
        if (not isinstance(self.port_capacity_classes, (list, tuple))
            or any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*',v)
                   for v in self.port_capacity_classes)
            or len(set(self.port_capacity_classes)) != len(self.port_capacity_classes)):
            raise ValueError('port_capacity_classes must be a list of distinct SVG class names')
        object.__setattr__(self, 'port_capacity_classes', tuple(self.port_capacity_classes))
        if self.min_terminal < 10:
            raise ValueError("min_terminal cannot be below the D2 elbow guard of 10px")

    @classmethod
    def from_mapping(cls, raw):
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("render.normalization must be a mapping")
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown normalization settings: {sorted(unknown)}")
        return cls(**raw)
