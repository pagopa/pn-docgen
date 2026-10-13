"""Logical paths from observed CFN membership, never from generated-name guesses."""
from collections import defaultdict


def stack_key(value: str) -> str:
    if not isinstance(value, str):
        return ''
    if value.startswith('arn:'):
        parts = value.split(':', 5)
        if len(parts) != 6 or parts[2] != 'cloudformation' or not parts[5].startswith('stack/'):
            return ''
        return parts[5].split('/')[1]
    return value


def logical_paths(resources: list[dict]) -> tuple[dict, list[dict]]:
    """Reconstruct after collection, independent of page and traversal order.

    Missing membership in old captures is not an error. Conflicting ownership,
    multiple parents and cycles abstain rather than selecting a first match.
    """
    members, slots, parents = defaultdict(set), defaultdict(set), defaultdict(set)
    for record in resources:
        identity = (record.get('type', ''), record.get('physical_id', ''))
        stack = stack_key(record.get('stack_name', ''))
        logical = record.get('logical_id', '')
        members[identity].add((stack, logical))
        if stack and logical:
            slots[stack, logical].add(identity)
        if identity[0] == 'AWS::CloudFormation::Stack' and stack and logical:
            child = stack_key(identity[1])
            if child:
                parents[child].add((stack, logical))

    paths, issues = {}, []
    for identity, locations in sorted(members.items()):
        if not any(stack for stack, _ in locations):
            continue  # Old capture: do not fabricate provenance from the ARN.
        reason = None
        if len(locations) != 1:
            reason = 'conflicting resource membership'
        else:
            stack, logical = next(iter(locations))
            path, visited = [logical], set()
            if not stack or not logical or len(slots[stack, logical]) != 1:
                reason = 'missing or conflicting logical slot'
            while not reason:
                if stack in visited:
                    reason = 'cyclic nested-stack ancestry'
                    break
                visited.add(stack)
                owners = parents.get(stack, set())
                if not owners:
                    break
                if len(owners) != 1:
                    reason = 'ambiguous nested-stack parent'
                    break
                parent, nested_logical = next(iter(owners))
                if len(slots[parent, nested_logical]) != 1:
                    reason = 'conflicting nested-stack logical slot'
                    break
                path.insert(0, nested_logical)
                stack = parent
            if not reason:
                paths[identity] = path
        if reason:
            issues.append({'resource': identity[1], 'reason': reason})
    return paths, issues
