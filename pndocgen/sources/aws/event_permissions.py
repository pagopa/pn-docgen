"""Extract explicit EventBridge Allow statements from already acquired policies."""
from copy import deepcopy
from fnmatch import fnmatchcase

from pndocgen.sources.aws.configured_references import concrete_identity


def collect(discoverer, document, source, source_type):
    statements = document.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
            continue
        actions = statement.get("Action", [])
        actions = [actions] if isinstance(actions, str) else actions
        if not isinstance(actions, list) or not any(
                isinstance(a, str) and fnmatchcase("events:putevents", a.lower()) for a in actions):
            continue
        resources = statement.get("Resource", [])
        resources = [resources] if isinstance(resources, str) else resources
        if not isinstance(resources, list) or not resources:
            discoverer._record_issue("unresolved_eventbridge_permission", source, "Missing concrete EventBus resource")
            continue
        for identifier in resources:
            ref = {"type": "AWS::Events::EventBus", "id": identifier}
            if not concrete_identity(ref):
                discoverer._record_issue("unresolved_eventbridge_permission", source,
                                         "Non-concrete EventBus resource: " + repr(identifier))
                continue
            edge = {"source": source, "source_type": source_type, "target": identifier,
                    "target_ref": ref, "type": "eventbridge_publish_permission",
                    "label": "PutEvents", "evidence": "iam_permission"}
            if "Condition" in statement:
                edge["evidence_details"] = {"condition": deepcopy(statement["Condition"])}
            if edge not in discoverer.eventbridge_publish_permissions:
                discoverer.eventbridge_publish_permissions.append(edge)
