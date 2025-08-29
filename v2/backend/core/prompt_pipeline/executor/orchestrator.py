# File: v2/backend/core/prompt_pipeline/executor/orchestrator.py
"""
Orchestrator: thin wrapper around the Spine capability registry.

- Expose `capability_run(name, payload, context=None)`.
- Resolve a runner from the Spine layer (robust to package-level name shadowing).
- Lazily load capabilities from config/spine/capabilities.yml on first use.
- Normalize provider return types to a list.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable, Dict, List, Optional, Tuple

_CAPS_LOADED = False  # ensure we only load capabilities once


# ------------------------ lazy capability loading ------------------------


def _ensure_caps_loaded() -> None:
    """Load capabilities into the Spine registry (once) from the YAML map."""
    global _CAPS_LOADED
    if _CAPS_LOADED:
        return
    try:
        # Preferred: let the spine.loader facade do it
        loader_mod = import_module("v2.backend.core.spine.loader")
        # calling get_registry() ensures the caps are loaded
        getattr(loader_mod, "get_registry")()
        _CAPS_LOADED = True
    except Exception:
        # Best-effort: still mark as loaded to avoid tight loops;
        # actual capability invocation will fail loudly if missing.
        _CAPS_LOADED = True


# ------------------------ registry resolution ------------------------


def _resolve_runner() -> Tuple[Optional[Callable[..., Any]], Optional[Any]]:
    """
    Locate a callable runner. Be robust to package-level name shadowing by
    importing the *modules* directly instead of `from package import registry`.
    """
    # 1) Try the registry module directly
    try:
        reg_mod = import_module("v2.backend.core.spine.registry")
        # Preferred function names
        fn = getattr(reg_mod, "run_capability", None)
        if callable(fn):
            return fn, reg_mod
        fn = getattr(reg_mod, "run", None)
        if callable(fn):
            return fn, reg_mod
        # Maybe there's a singleton object with .run
        reg_obj = getattr(reg_mod, "registry", None)
        if reg_obj is not None:
            run_m = getattr(reg_obj, "run", None)
            if callable(run_m):
                return run_m, reg_obj
    except Exception:
        pass

    # 2) Try the loader module facade
    try:
        loader_mod = import_module("v2.backend.core.spine.loader")
        fn = getattr(loader_mod, "capability_run", None)
        if callable(fn):
            return fn, loader_mod
        reg = getattr(loader_mod, "REGISTRY", None)
        if reg is not None and callable(getattr(reg, "run", None)):
            return getattr(reg, "run"), reg
        get_reg = getattr(loader_mod, "get_registry", None)
        if callable(get_reg):
            reg2 = get_reg()
            run_m2 = getattr(reg2, "run", None)
            if callable(run_m2):
                return run_m2, reg2
    except Exception:
        pass

    return None, None


# ---------------------------- coercion utils ----------------------------


def _to_artifacts_list(res: Any) -> List[Any]:
    """Coerce any provider result into a list."""
    if res is None:
        return []
    if isinstance(res, list):
        return res
    if isinstance(res, tuple):
        return list(res)
    return [res]


# ------------------------------ public API ------------------------------


def capability_run(
    name: str,
    payload: Dict[str, Any] | Any,
    context: Optional[Dict[str, Any]] = None,
) -> List[Any]:
    """
    Execute a Spine capability by name and normalize the result into a list.

    Retry only on TypeError (signature mismatch); raise real provider errors immediately.
    """
    _ensure_caps_loaded()

    runner, _ = _resolve_runner()
    if runner is None:
        raise RuntimeError("Spine registry runner not available (could not resolve a run function)")

    try:
        return _to_artifacts_list(runner(name, payload, context))
    except TypeError:
        # Try without context
        try:
            return _to_artifacts_list(runner(name, payload))
        except TypeError:
            # Try keyword-only form (some facades use keywords)
            return _to_artifacts_list(runner(name=name, payload=payload, context=context))




