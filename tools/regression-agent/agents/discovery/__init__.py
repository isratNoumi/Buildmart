"""Discovery Agent (plan/discovery.md).

Produces a structured, evidence-backed Application Knowledge Model for any
web application. Run with: python -m agents.discovery --help
"""

from .config import DiscoveryRequest

__all__ = ["DiscoveryRequest"]
