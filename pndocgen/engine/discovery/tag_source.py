"""
pn-docgen - Tag Discovery Source Strategy

Assigns resources to components based on AWS resource tags.
Default tag key: Microservice (set by pn-cicd deployEcsService.sh)

Usage:
    strategy = TagDiscoverySource(tag_key="Microservice")
    component = strategy.assign_component(node)  # -> "pn-delivery" or None
"""

from typing import Optional

from pndocgen.engine.core.models import InventoryNode
from pndocgen.engine.discovery import DiscoverySourceStrategy


class TagDiscoverySource(DiscoverySourceStrategy):
    """
    Assigns resources to components using AWS resource tags.
    Looks for tag_key in the node's tags dict.
    Falls back to secondary_keys if primary not found.
    """

    name = "tag"

    def __init__(
        self,
        tag_key: str = "Microservice",
        secondary_keys: list[str] | None = None,
    ):
        self.tag_key = tag_key
        # Fallback tag keys for legacy resources
        self.secondary_keys = secondary_keys or ["Component", "Service", "microservice"]

    def assign_component(self, node: InventoryNode) -> Optional[str]:
        tags = node.tags or {}

        # Primary key
        val = tags.get(self.tag_key)
        if val:
            return val.strip()

        # Secondary keys (legacy fallback)
        for key in self.secondary_keys:
            val = tags.get(key)
            if val:
                return val.strip()

        return None
