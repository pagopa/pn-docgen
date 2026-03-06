"""
pn-docgen - CFN Discovery Source Strategy

Assigns resources to components based on CloudFormation stack membership.
Uses CfnLiveDiscoverer's pre-built resource_map to look up component names.

This strategy is more accurate than tag-based because:
  - Covers resources that don't have the Microservice tag
  - Handles nested stacks automatically
  - Uses PhysicalResourceId (real deployed names)

Usage:
    discoverer = CfnLiveDiscoverer(session, region)
    discoverer.discover()

    strategy = CfnDiscoverySource(discoverer)
    component = strategy.assign_component(node)  # -> "pn-delivery" or None
"""

from typing import Optional

import boto3

from pndocgen.engine.core.models import InventoryNode
from pndocgen.engine.discovery import DiscoverySourceStrategy
from pndocgen.sources.aws.cfn_discoverer import CfnLiveDiscoverer


class CfnDiscoverySource(DiscoverySourceStrategy):
    """
    Assigns resources to components using CloudFormation stack membership.
    Requires a pre-populated CfnLiveDiscoverer.
    """

    name = "cfn"

    def __init__(self, discoverer: CfnLiveDiscoverer):
        self._discoverer = discoverer

    def prepare(self, session: boto3.Session) -> None:
        """Run CFN discovery if not already done."""
        if not self._discoverer.resource_map:
            self._discoverer.discover()

    def assign_component(self, node: InventoryNode) -> Optional[str]:
        """Look up the node's physical ID in the CFN resource map."""
        # node.id is the ARN or physical resource id
        if node.id and node.id in self._discoverer.resource_map:
            return self._discoverer.resource_map[node.id]
        # Try name (queue name, function name, etc.)
        if node.name and node.name in self._discoverer.resource_map:
            return self._discoverer.resource_map[node.name]
        return None


class CompositeDiscoverySource(DiscoverySourceStrategy):
    """
    Combines multiple strategies: tries each in order, returns first match.
    Typical use: TagDiscoverySource primary, CfnDiscoverySource fallback.

    Example:
        strategy = CompositeDiscoverySource([
            TagDiscoverySource(tag_key="Microservice"),
            CfnDiscoverySource(discoverer),
        ])
    """

    name = "composite"

    def __init__(self, strategies: list[DiscoverySourceStrategy]):
        self._strategies = strategies

    def prepare(self, session: boto3.Session) -> None:
        for s in self._strategies:
            s.prepare(session)

    def assign_component(self, node: InventoryNode) -> Optional[str]:
        for strategy in self._strategies:
            result = strategy.assign_component(node)
            if result:
                return result
        return None
