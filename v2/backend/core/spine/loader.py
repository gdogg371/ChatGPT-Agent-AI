# File: v2/backend/core/spine/loader.py
"""
Spine loader and facade (module-level singleton).

- Loads capability mappings from a YAML file into the existing registry
  (CapabilityRegistry + REGISTRY singleton defined in spine.registry).
- Provides `capability_run(name, payload, context=None)` and `get_registry()`.
- Stays domain-agnostic; no pipeline/docstring-specific code here.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# Import the actual registry API you already have
from .registry import CapabilityRegistry, REGISTRY as _REGISTRY_SINGLETON  # type: ignore


# ------------------------------- YAML loader ---------------------------------


class CapabilitiesLoader:
    """
    Load capability → provider mapping from a YAML file into a registry.

    YAML shape:
      capability.name.v1:
        target: "module.path:function"
        # (optional metadata keys ignored here)
    """

    def __init__(self, caps_path: Path | str) -> None:
        self.caps_path = Path(caps_path).expanduser().resolve()

    def _load_yaml(self) -> Dict[str, Any]:
        # Lazy import so this module imports even if PyYAML isn't installed yet.
        try:
            import yaml  # type: ignore
        except Exception as e:
            raise RuntimeError("PyYAML is required to load Spine capabilities (pip install pyyaml)") from e

        text = self.caps_path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("capabilities.yml must contain a mapping at top-level")
        return data

    @staticmethod
    def _resolve_target(spec: str) -> Callable[..., Any]:
        """Resolve 'module.submodule:callable' into a Python callable."""
        if not isinstance(spec, str) or ":" not in spec:
            raise ValueError(f"Invalid target spec (expected 'module:callable'): {spec!r}")
        mod_name, fn_name = spec.split(":", 1)
        mod = import_module(mod_name)
        fn = getattr(mod, fn_name, None)
        if not callable(fn):
            raise AttributeError(f"Target {spec!r} is not callable")
        return fn

    def load(self, registry: CapabilityRegistry) -> None:
        """Parse YAML and register each capability target into `registry`."""
        data = self._load_yaml()
        for cap_name, entry in data.items():
            if not isinstance(entry, dict):
                continue
            target = entry.get("target")
            if not target:
                continue  # allow comment-only stanzas
            fn = self._resolve_target(str(target))
            # Your CapabilityRegistry.register(name, fn) expects a callable
            registry.register(str(cap_name), fn)


# ----------------------------- Facade functions ------------------------------


# Public alias expected by other modules
REGISTRY: CapabilityRegistry = _REGISTRY_SINGLETON


def capability_run(name: str, payload: Any, context: Optional[Dict[str, Any]] = None) -> Any:
    """
    Facade for executing a capability by name via the singleton registry.

    Returns the registry's normalized Artifact list (or provider-native result).
    """
    return REGISTRY.run(name, payload, context)


def get_registry() -> CapabilityRegistry:
    """Return the process-wide capability registry singleton."""
    return REGISTRY

