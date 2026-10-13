"""Deterministic presentation labels, independent of IDs and edge endpoints."""
from collections import defaultdict
import re


def humanize(value: str) -> str:
    value = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', value)
    return re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', value)


def _key(value: str) -> str:
    return ' '.join(value.split()).casefold()


def choose_labels(nodes, fallbacks: dict[str, str], prefixes=None) -> tuple[dict[str, str], list[dict]]:
    """Shortest unique formatted logical suffix, expanded simultaneously.

    Legacy nodes are fixed obstacles and retain their existing labels. New
    logical labels must be unique against them as well as each other. Rules
    are type-derived, with no component names or random/order-based suffixes.
    """
    options, indices, managed = {}, {}, set()
    prefixes = prefixes or {}
    for node in nodes:
        prefix = prefixes.get(node.name, '')
        options[node.name] = [(prefix + fallbacks[node.name], 'legacy')]
        indices[node.name] = 0
        metadata = node.metadata
        path = metadata.get('cfn_logical_path')
        if (metadata.get('cfn_provenance') != 'stack_membership_v1' or not isinstance(path, list)
                or not path or any(not isinstance(p, str) or not re.fullmatch(r'[A-Za-z0-9]+', p) for p in path)
                or path[-1] != metadata.get('logical_id')):
            continue
        kind = metadata.get('cfn_type', '').split('::')
        generic = {'Resource', 'Default'}
        if len(kind) == 3:
            generic.update((kind[-1], kind[-2] + kind[-1]))
        meaningful = path[:-1] if path[-1] in generic else path
        if not meaningful:
            continue
        candidates = [(' / '.join(humanize(p) for p in meaningful[-depth:]), 'logical_path')
                      for depth in range(1, len(meaningful) + 1)]
        candidates.extend([(fallbacks[node.name], 'technical_fallback'), (node.name, 'technical_name'),
                           (node.id, 'physical_identity')])
        # Formatting can make different strings equivalent. Collapse them
        # before the collision loop so every expansion makes bounded progress.
        seen = set()
        options[node.name] = []
        for label, source in candidates:
            label = prefix + label
            if _key(label) not in seen:
                options[node.name].append((label, source))
                seen.add(_key(label))
        managed.add(node.name)

    while True:
        groups = defaultdict(list)
        for name in sorted(options):
            groups[_key(options[name][indices[name]][0])].append(name)
        conflicts = [group for group in groups.values() if len(group) > 1 and any(n in managed for n in group)]
        if not conflicts:
            break
        progress = False
        for group in conflicts:
            for name in group:
                if name in managed and indices[name] + 1 < len(options[name]):
                    indices[name] += 1
                    progress = True
        if not progress:
            raise ValueError('Cannot assign unambiguous display labels without changing resource identity')
    labels = {name: options[name][indices[name]][0] for name in sorted(options)}
    decisions = [{'name': name, 'label': labels[name], 'source': options[name][indices[name]][1]}
                 for name in sorted(managed)]
    return labels, decisions
