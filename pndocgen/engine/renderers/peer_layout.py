"""Transparent layout scopes for independent peers in mixed leaf containers.

Only generator-owned D2 is accepted. Resource kinds are opaque grouping keys;
neither AWS services nor component names are part of the layout policy.
"""
from collections import defaultdict
import re


def group_mixed_peers(text, clusters, data_edges, contracts):
    """Return D2, contracts and endpoint projection without changing the graph.

    A column containing cross-type internal edges has two layout responsibilities:
    align independent same-type peers, and route the internal flow. Nested
    invisible scopes let ELK handle those responsibilities separately.
    """
    projected = {}
    result_contracts = {name: dict(rule) for name, rule in contracts.items()}
    for cluster, nodes in sorted(clusters.items()):
        if contracts.get(cluster, {}).get("layout") != "column":
            continue
        kinds = {f"{cluster}.{node['id']}": node["resource_type"] for node in nodes}
        internal = [(edge["from_path"], edge["to_path"]) for edge in data_edges
                    if edge["from_path"] in kinds and edge["to_path"] in kinds]
        if not any(kinds[a] != kinds[b] for a, b in internal):
            continue
        by_kind = defaultdict(list)
        for node in nodes:
            by_kind[node["resource_type"]].append(node["id"])
        if all(len(members) == 1 for members in by_kind.values()):
            # Different singletons connected internally form a flow, not a
            # peer column. Keep ELK routing; there are no same-type peers to align.
            result_contracts[cluster] = {"layout": "free", "columns": 1}
            continue
        groups = [members for _, members in sorted(by_kind.items())
                  if len(members) > 1 and not any(
                      a.rsplit(".", 1)[-1] in members and b.rsplit(".", 1)[-1] in members
                      for a, b in internal)]
        if not groups:
            continue
        pattern = rf'^{re.escape(cluster)}: "[^"\n]*" \{{\n.*?^\}}\n'
        block = re.search(pattern, text, re.M | re.S)
        if block is None:
            raise ValueError(f"Cannot locate generated leaf container: {cluster}")
        body = block[0]
        occupied = {node["id"] for node in nodes}
        for index, members in enumerate(groups):
            scope = f"_layout_peers_{index}"
            while scope in occupied:
                scope += "_"
            occupied.add(scope)
            node_blocks = []
            for member in members:
                match = re.search(rf'^  {re.escape(member)}: "[^\n]*" \{{\n.*?^  \}}\n',
                                  body, re.M | re.S)
                if match is None:
                    raise ValueError(f"Cannot locate generated peer: {cluster}.{member}")
                node_blocks.append(match[0])
            wrapper = (f'  {scope}: "" {{\n    class: boundary\n'
                       '    style.stroke-width: 0\n    style.fill: transparent\n'
                       '    direction: down\n')
            wrapper += "".join("  " + line + "\n" for part in node_blocks
                               for line in part.splitlines()) + "  }\n"
            body = body.replace(node_blocks[0], wrapper, 1)
            for part in node_blocks[1:]:
                body = body.replace(part, "", 1)
            path = f"{cluster}.{scope}"
            result_contracts[path] = {"layout": "column", "columns": 1}
            projected.update({f"{cluster}.{member}": f"{path}.{member}" for member in members})
        # The parent now owns routing and nested groups, not a single leaf column.
        result_contracts.pop(cluster)
        text = text[:block.start()] + body + text[block.end():]
    if projected:
        text = re.sub(r"^([\w.]+) -> ([\w.]+)(?=[: {])",
                      lambda m: f"{projected.get(m[1], m[1])} -> {projected.get(m[2], m[2])}",
                      text, flags=re.M)
    return text, result_contracts, projected
