"""Capture selected rule configuration without simulating EventBridge delivery."""
import json
import re

from pndocgen.sources.aws.dataflows import add_link


def enrich_rule(discoverer, entry):
    key = "eventbridge:" + entry["physical_id"]
    captured = discoverer.dataflow_descriptions.setdefault(key, {})
    if captured.get("_rule_described") is not True:
        params = {"Name": entry["rule_api_name"]}
        if entry["event_bus_name"]:
            params["EventBusName"] = entry["event_bus_name"]
        try:
            response = discoverer.events.describe_rule(**params)
            captured.update({k: response[k] for k in
                             ("Arn", "EventBusName", "State", "EventPattern", "ScheduleExpression")
                             if k in response})
            captured["_rule_described"] = True
        except Exception as exc:
            discoverer._record_issue("describe_rule", entry["physical_id"], exc)
    rule_arn = captured.get("Arn", "")
    match = re.fullmatch(r"arn:(aws(?:-cn|-us-gov)?):events:([a-z0-9-]+):(\d{12}):rule/(.+)", rule_arn)
    bus = captured.get("EventBusName")
    if not bus:
        return
    if bus.startswith("arn:"):
        bus_arn = bus
    elif match:
        bus_arn = f"arn:{match[1]}:events:{match[2]}:{match[3]}:event-bus/{bus}"
    else:
        discoverer._record_issue("unresolved_rule_bus", entry["physical_id"],
                                 "Rule bus has no captured account/partition scope")
        return
    add_link(discoverer, "AWS::Events::EventBus", bus_arn,
             "AWS::Events::Rule", entry["physical_id"], "eventbridge_rule_association",
             "routes", "eventbridge_rule_configuration")


def metadata(discoverer, kind, identifier):
    if kind == 'AWS::Lambda::Function':
        from pndocgen.sources.aws.resource_identity import lambda_identity
        identity = lambda_identity(identifier)
        scopes = [s for s in discoverer.dataflow_scopes if s[1] == discoverer.region]
        if not identity and len(scopes) == 1 and re.fullmatch(r'[A-Za-z0-9_-]+', identifier):
            partition, region, account = scopes[0]
            identity = lambda_identity(f'arn:{partition}:lambda:{region}:{account}:function:{identifier}')
        return {'resource_identity': {'type': kind, 'id': identity}} if identity else {}
    if kind == "AWS::Events::EventBus":
        from pndocgen.sources.aws.configured_references import concrete_identity
        ref = {"type": kind, "id": identifier}
        if concrete_identity(ref):
            return {"resource_identity": ref}
        scopes = [s for s in discoverer.dataflow_scopes if s[1] == discoverer.region]
        if len(scopes) == 1 and re.fullmatch(r"[A-Za-z0-9._/-]+", identifier):
            partition, region, account = scopes[0]
            ref["id"] = f"arn:{partition}:events:{region}:{account}:event-bus/{identifier}"
            if concrete_identity(ref):
                return {"resource_identity": ref}
        return {}
    if kind == "AWS::Events::Rule":
        captured = discoverer.dataflow_descriptions.get("eventbridge:" + identifier, {})
        result = {key: captured[key] for key in ("EventBusName", "State", "ScheduleExpression")
                  if key in captured}
        if "EventPattern" in captured:
            pattern = captured["EventPattern"]
            try:
                pattern = json.loads(pattern) if isinstance(pattern, str) else pattern
            except (ValueError, TypeError):
                discoverer._record_issue("invalid_event_pattern", identifier, "Captured pattern is not valid JSON")
            result["EventPattern"] = pattern
        return {"eventbridge_rule": result} if result else {}
    if kind == "AWS::Pipes::Pipe":
        captured = discoverer.dataflow_descriptions.get(identifier, {})
        result = {key: captured[key] for key in ("CurrentState", "DesiredState", "Source", "Target")
                  if key in captured}
        return {"eventbridge_pipe": result} if result else {}
    return {}
