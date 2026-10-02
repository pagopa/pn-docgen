"""Emit an invisible ingress peer band using the validated ELK layout."""
import json
import re
from pndocgen.engine.renderers.svg_layout import CONTRACT_PREFIX


def group_ingress(text):
    """Final emission pass for known generator templates, not arbitrary D2.

    Scope only the existing input blocks and their endpoints; never edit IR.
    """
    names = ("apigw", "rules", "queues")
    contracts = json.loads(text.splitlines()[0][len(CONTRACT_PREFIX):])
    if "ingress" in contracts["clusters"] or any(k.startswith("ingress.") for k in contracts["clusters"]):
        return text
    blocks = {}
    # Adapter deliberately restricted to blocks emitted by our templates.
    for match in re.finditer(r'^([A-Za-z_]\w*): "[^"\n]*" \{\n.*?^\}\n', text, re.M | re.S):
        if match[1] in names:
            blocks[match[1]] = match[0]
    if len(blocks) < 2:
        return text
    insertion = "# ingress-role-band-placeholder"
    text = text.replace(next(iter(blocks.values())), insertion + "\n" + next(iter(blocks.values())), 1)
    for name, block in blocks.items():
        text = text.replace(block, "", 1)
        contracts["clusters"]["ingress." + name] = contracts["clusters"].pop(name)
    # Move scopes and rewrite endpoints, never add a semantic connection.
    for name in blocks:
        text = re.sub(rf'(?m)^{re.escape(name)}(?=\.| ->)', "ingress." + name, text)
        text = re.sub(rf'(?<= -> ){re.escape(name)}(?=\.|[ :{{\n])', "ingress." + name, text)
    lines = text.splitlines()
    lines[0] = CONTRACT_PREFIX + json.dumps(contracts, sort_keys=True, separators=(",", ":"))
    wrapper = '\ningress: "" {\n  class: boundary\n  style.stroke-width: 0\n  style.fill: transparent\n'
    wrapper += "  direction: down\n"
    for name in names:
        if name in blocks:
            wrapper += "\n".join("  " + line for line in blocks[name].splitlines()) + "\n"
    return "\n".join(lines).replace(insertion, wrapper + "}") + "\n"
