# SPDX-License-Identifier: MIT
# File: v2/backend/core/spine/registry.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Protocol

from .contracts import Task, Artifact


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """
    Declares a capability the provider supports and optional selectors for routing.
    Example:
      CapabilityDescriptor(
         name="analyze.docstrings.v1",
         selectors={"language": "python"}
      )
    """
    name: str
    selectors: Dict[str, Any]


class Provider(Protocol):
    """
    Minimal provider protocol the registry routes to.
    Providers are thin adapters over existing modules.
    """
    name: str
    version: str
    capabilities: List[CapabilityDescriptor]

    def handle(self, task: Task) -> List[Artifact] | Artifact:
        ...


class Registry:
    """
    Very small capability registry. In this first pass we keep selection simple:
    - exact capability name match
    - first-registered wins (you can extend this with selectors/policy later)
    """
    def __init__(self) -> None:
        self._providers: Dict[str, List[Provider]] = {}

    def register(self, p: Provider) -> None:
        for cap in p.capabilities:
            self._providers.setdefault(cap.name, []).append(p)

    def providers_for(self, capability: str) -> List[Provider]:
        return self._providers.get(capability, [])

    def resolve(self, capability: str) -> Provider:
        provs = self.providers_for(capability)
        if not provs:
            raise RuntimeError(f"CapabilityUnavailable: {capability}")
        return provs[0]
