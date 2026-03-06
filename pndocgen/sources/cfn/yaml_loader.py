"""
pn-docgen - CFN-aware YAML Loader

Shared utility for loading CloudFormation YAML files.
Handles all CFN intrinsic functions (!Sub, !Ref, !GetAtt, !If, etc.)
by treating them as plain Python values.
"""

import re
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class _CfnLoader(yaml.SafeLoader):
    """
    YAML SafeLoader subclass that handles CloudFormation intrinsic functions.
    All CFN tags are preserved as plain Python scalars, lists, or dicts.
    """
    pass


def _cfn_any(loader: yaml.SafeLoader, node: yaml.Node):
    """Generic constructor: preserves value regardless of CFN tag."""
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return None


# Register all known CFN intrinsic function tags
_CFN_TAGS = [
    "!Sub", "!Ref", "!GetAtt", "!If", "!Not", "!Equals",
    "!And", "!Or", "!Join", "!Select", "!Split", "!FindInMap",
    "!Condition", "!ImportValue", "!Base64", "!Cidr",
    "!Transform", "!GetAZs",
]
for _tag in _CFN_TAGS:
    _CfnLoader.add_constructor(_tag, _cfn_any)

# Catch-all for any other unknown CFN tags
_CfnLoader.add_multi_constructor(
    "!", lambda loader, tag, node: _cfn_any(loader, node)
)


def load_yaml_cfn(path: Path) -> dict:
    """Load a CloudFormation YAML file, handling all intrinsic functions."""
    try:
        with open(path) as f:
            return yaml.load(f, Loader=_CfnLoader) or {}
    except Exception as e:
        logger.warning("Cannot load CFN YAML %s: %s", path, e)
        return {}


def resolve_cfn_sub(value: str, project_name: str) -> str:
    """
    Resolve simple CloudFormation !Sub expressions.
    Replaces ${ProjectName} and other known pseudo-parameters.
    Remaining ${...} placeholders are replaced with '*'.
    """
    if not value:
        return ""
    result = str(value)
    result = result.replace("${ProjectName}", project_name)
    result = result.replace("${AWS::Region}", "eu-south-1")
    result = result.replace("${AWS::AccountId}", "ACCOUNT")
    result = re.sub(r"\$\{[^}]+\}", "*", result)
    return result.strip("'\"")
