"""Explicit, opt-in exclusions from the rendered view, never from the inventory."""
from dataclasses import dataclass, field

PURPOSES = frozenset({'application', 'tracing', 'logging', 'test'})


@dataclass(frozen=True)
class PurposeRule:
    resource_type: str
    name_prefix: str
    purpose: str

    def matches(self, node):
        return node.resource_type == self.resource_type and node.name.startswith(self.name_prefix)


@dataclass(frozen=True)
class Exclusion:
    component: str
    resource_type: str
    name_prefix: str

    def matches(self, component, node):
        return (component == self.component and node.resource_type == self.resource_type
                and node.name.startswith(self.name_prefix))


@dataclass
class ViewConfig:
    exclusions: list[Exclusion] = field(default_factory=list)
    edge_exclusions: list["EdgeExclusion"] = field(default_factory=list)
    excluded_clusters: list[str] = field(default_factory=lambda: ["security", "misc"])
    excluded_purposes: list[str] = field(default_factory=lambda: ['logging', 'test'])
    purpose_rules: list[PurposeRule] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, value):
        if value is None:
            return cls()
        if not isinstance(value, dict) or set(value) - {"exclusions", "edge_exclusions", "excluded_clusters", "excluded_purposes", "purpose_rules"}:
            raise ValueError("Unknown render.view setting")
        purposes = value.get('excluded_purposes', ['logging', 'test'])
        if not isinstance(purposes, list) or any(not isinstance(v, str) or v not in PURPOSES for v in purposes):
            raise ValueError('excluded_purposes accepts application, tracing, logging, test')
        role_rules = value.get('purpose_rules', [])
        if not isinstance(role_rules, list):
            raise ValueError('purpose_rules must be a list')
        roles = []
        for rule in role_rules:
            if (not isinstance(rule, dict) or set(rule) != {'resource_type', 'name_prefix', 'purpose'}
                    or any(not isinstance(v, str) or not v.strip() or v != v.strip() for v in rule.values())
                    or rule['purpose'] not in PURPOSES):
                raise ValueError('purpose_rules require resource_type, name_prefix and a supported purpose')
            roles.append(PurposeRule(**rule))
        clusters = value.get("excluded_clusters", ["security", "misc"])
        if (not isinstance(clusters, list)
                or any(not isinstance(v, str) or not v.strip() or v != v.strip()
                       for v in clusters)):
            raise ValueError("render.view.excluded_clusters must be a list of nonempty cluster names")
        rules = value.get("exclusions", [])
        if not isinstance(rules, list):
            raise ValueError("render.view.exclusions must be a list")
        parsed = []
        for rule in rules:
            fields = {"component", "resource_type", "name_prefix"}
            if (not isinstance(rule, dict) or set(rule) != fields
                    or any(not isinstance(v, str) or not v.strip() or v != v.strip()
                           for v in rule.values())):
                raise ValueError("Each exclusion requires nonempty component, resource_type, name_prefix")
            parsed.append(Exclusion(**rule))
        edge_rules = value.get("edge_exclusions", [])
        if not isinstance(edge_rules, list):
            raise ValueError("render.view.edge_exclusions must be a list")
        edges = []
        for rule in edge_rules:
            if (not isinstance(rule, dict)
                    or not {"component", "edge_type"} <= set(rule)
                    or set(rule) - {"component", "edge_type", "source", "target"}
                    or any(not isinstance(v, str) or not v.strip() or v != v.strip()
                           for v in rule.values())):
                raise ValueError("Each edge exclusion requires component and edge_type; optional source/target must be nonempty exact names")
            edges.append(EdgeExclusion(**rule))
        return cls(sorted(set(parsed), key=lambda r: (r.component, r.resource_type, r.name_prefix)),
                   sorted(set(edges), key=lambda r: (r.component, r.edge_type, r.source or "", r.target or "")),
                   list(dict.fromkeys(clusters)), sorted(set(purposes)),
                   sorted(set(roles), key=lambda r: (r.resource_type, r.name_prefix, r.purpose)))


@dataclass(frozen=True)
class EdgeExclusion:
    component: str
    edge_type: str
    source: str | None = None
    target: str | None = None

    def matches(self, component, edge):
        return (component == self.component and edge.edge_type == self.edge_type
                and (self.source is None or edge.source == self.source)
                and (self.target is None or edge.target == self.target))

    def to_dict(self):
        return {key: value for key, value in vars(self).items() if value is not None}
