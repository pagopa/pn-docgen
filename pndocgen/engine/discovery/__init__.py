"""
pn-docgen - Discovery Source Strategy Interface

A DiscoverySourceStrategy determines HOW resources are grouped into components.
Different infrastructures use different approaches:
  - tag-based: AWS resource tags (e.g. Microservice=pn-delivery)
  - cfn-based: CloudFormation stack membership (DescribeStackResources)
  - both: tag primary, cfn as fallback/enrichment

To add a new strategy:
  1. Inherit from DiscoverySourceStrategy
  2. Implement assign_component(resource) -> Optional[str]
  3. Pass it to TagResolver(strategy=MyStrategy())
"""

from abc import ABC, abstractmethod
from typing import Optional

from pndocgen.engine.core.models import InventoryNode


class DiscoverySourceStrategy(ABC):
    """
    Interface for component assignment strategies.
    Given an InventoryNode, returns the component name it belongs to (or None).
    """

    name: str = "unnamed"

    @abstractmethod
    def assign_component(self, node: InventoryNode) -> Optional[str]:
        """
        Return the component name for this resource, or None if unknown.
        The TagResolver will fall back to name-prefix matching if None is returned.
        """
        ...

    def prepare(self, session) -> None:
        """
        Optional: called once before processing starts.
        Use to pre-fetch data (e.g. CFN stack membership maps).
        session: boto3.Session
        """
        pass
