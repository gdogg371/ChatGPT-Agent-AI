# File: v2/backend/core/spine/__init__.py
from __future__ import annotations

"""
Spine public API.

Exports:
- Spine façade + helpers to build it
- Registry and capabilities loader
- Validation registry + convenience validators
- Core contracts (Artifact, Task, Envelope, Problem, new_envelope, to_dict)
"""

# Façade + builders
from .bootstrap import (
    Spine,
    load_middlewares_from_env,
    setup_registry,
    build_spine,
)

# Registry & capability loading
from .registry import Registry
from .loader import CapabilitiesLoader

# Validation
from .validation import (
    ValidatorRegistry,
    VALIDATORS,
    require_fields,
    optional_fields,
    is_dict,
    is_list,
    is_instance,
)

# Contracts (lightweight data classes)
from .contracts import (
    Artifact,
    Task,
    Envelope,
    Problem,
    new_envelope,
    to_dict,
)

__all__ = [
    # façade + builders
    "Spine",
    "load_middlewares_from_env",
    "setup_registry",
    "build_spine",
    # registry + loader
    "Registry",
    "CapabilitiesLoader",
    # validation
    "ValidatorRegistry",
    "VALIDATORS",
    "require_fields",
    "optional_fields",
    "is_dict",
    "is_list",
    "is_instance",
    # contracts
    "Artifact",
    "Task",
    "Envelope",
    "Problem",
    "new_envelope",
    "to_dict",
]


if __name__ == "__main__":
    """
    Static import smoke test for this package.
    Run:
        python v2\\backend\\core\\spine\\__init__.py
    """
    # Simple object creations to ensure symbols import and link correctly.
    reg = Registry()
    assert isinstance(reg, Registry)

    # Build a Spine from an empty temp capabilities file (no dispatch).
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        yml = Path(td) / "caps.yml"
        yml.write_text("", encoding="utf-8")
        # Empty caps are fine for construction (no dispatch in this smoke test)
        s = Spine.from_registry(reg)
        assert isinstance(s, Spine)

    print("[spine.__init__] OK")



