"""Conservative, offline extraction of direct CFN Scheduler→Lambda targets.

Only a literal GetAtt of a Lambda ARN in the same template is supported.
Anything else is disclosed and left unresolved; no ARN or name heuristics.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re

import yaml

from pndocgen.engine.core.config import AppConfig
from pndocgen.sources.cfn.yaml_loader import _CfnLoader, _cfn_any


_DIRECT_ARN = re.compile(r"([A-Za-z][A-Za-z0-9]*)\.Arn")


class _ScheduleLoader(_CfnLoader):
    """Preserve GetAtt's identity; the generic CFN loader erases its YAML tag."""


def _getatt(loader: yaml.SafeLoader, node: yaml.Node) -> dict:
    return {"Fn::GetAtt": _cfn_any(loader, node)}


_ScheduleLoader.add_constructor("!GetAtt", _getatt)


def _direct_getatt_arn(value) -> str | None:
    if not isinstance(value, dict) or set(value) != {"Fn::GetAtt"}:
        return None
    value = value["Fn::GetAtt"]
    if isinstance(value, list) and len(value) == 2:
        logical_id, attribute = value
        if isinstance(logical_id, str) and attribute == "Arn":
            return logical_id
        return None
    if isinstance(value, str):
        match = _DIRECT_ARN.fullmatch(value.strip())
        return match.group(1) if match else None
    return None


def scan_schedule_bindings(repo_path: Path, config: AppConfig) -> tuple[list[dict], list[dict]]:
    """Inspect configured CFN templates; return bindings and per-file status.

    Records with ``status=direct_getatt_lambda`` are *candidates* for a graph
    edge, contingent on unique logical-ID matches in the captured inventory.
    The template is source evidence, not proof of the deployed target.
    """
    configured = [str(value).strip() for value in config.discovery.cfn_analyzer.template_files
                  if str(value).strip()]
    if not configured:
        configured = list(dict.fromkeys(
            str(value).strip() for value in config.discovery.cfn_paths.values()
            if str(value).strip()
        ))
    records = []
    sources = []
    repo_root = repo_path.resolve()
    for relative in configured:
        source = {"source_file": relative}
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            sources.append({**source, "status": "template_path_outside_repo"})
            continue
        template = repo_path / relative_path
        if not template.exists():
            sources.append({**source, "status": "template_missing"})
            continue
        if not template.is_file():
            sources.append({**source, "status": "template_not_file"})
            continue
        if not template.resolve().is_relative_to(repo_root):
            sources.append({**source, "status": "template_path_outside_repo"})
            continue
        try:
            template_bytes = template.read_bytes()
        except OSError:
            sources.append({**source, "status": "template_unreadable"})
            continue
        template_sha256 = sha256(template_bytes).hexdigest()
        source["template_sha256"] = template_sha256
        try:
            document = yaml.load(template_bytes, Loader=_ScheduleLoader)
        except yaml.YAMLError:
            sources.append({**source, "status": "template_parse_error"})
            continue
        if not isinstance(document, dict):
            sources.append({**source, "status": "template_invalid_root"})
            continue
        resources = document.get("Resources")
        if not isinstance(resources, dict):
            sources.append({**source, "status": "template_invalid_resources"})
            continue
        schedule_count = 0
        for logical_id, resource in resources.items():
            if not isinstance(resource, dict) or resource.get("Type") != "AWS::Scheduler::Schedule":
                continue
            schedule_count += 1
            properties = resource.get("Properties", {}) or {}
            target = properties.get("Target", {}) if isinstance(properties, dict) else {}
            target_arn = target.get("Arn") if isinstance(target, dict) else None
            target_logical_id = _direct_getatt_arn(target_arn)
            status = "unsupported_target_expression"
            if target_logical_id:
                target_resource = resources.get(target_logical_id, {})
                status = ("direct_getatt_lambda" if isinstance(target_resource, dict)
                          and target_resource.get("Type") == "AWS::Lambda::Function"
                          else "target_not_local_lambda")
            records.append({
                "schedule_logical_id": logical_id,
                "target_logical_id": target_logical_id,
                "source_file": relative,
                "template_sha256": template_sha256,
                "status": status,
            })
        sources.append({**source, "status": "scanned", "schedule_count": schedule_count})
    return sorted(records, key=lambda row: (
        str(row["schedule_logical_id"]), str(row["target_logical_id"] or ""), row["source_file"]
    )), sources
