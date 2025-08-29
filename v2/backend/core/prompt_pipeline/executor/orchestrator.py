# File: v2/backend/core/prompt_pipeline/executor/orchestrator.py
"""
Orchestrator: thin wrapper around the Spine capability registry.

- Provides `capability_run(name, payload, context=None)` which:
  * Looks up a runner function from the Spine registry.
  * Calls it with best-effort signatures to avoid tight coupling.
  * Normalizes the result into a list so callers (engine) can safely do `arts[0]`.

This module is intentionally domain-agnostic.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple


# ------------------------ registry resolution ------------------------

def _resolve_runner() -> Tuple[Optional[Callable[..., Any]], Optional[Any]]:
    """
    Try to locate a runner callable from the Spine layer.

    Supported forms (any one may exist depending on the version):
      - spine.registry.run_capability(name, payload, context)
      - spine.registry.run(name, payload, context)
      - spine.registry.get_registry().run(name, payload, context)
      - spine.loader.get_registry().run(name, payload, context)
    """
    # 1) Try explicit exported functions
    try:
        from v2.backend.core.spine import registry as _reg  # type: ignore
        fn = getattr(_reg, "run_capability", None)
        if callable(fn):
            return fn, _reg
        fn = getattr(_reg, "run", None)
        if callable(fn):
            return fn, _reg
        # Might expose a 'registry' object with a 'run' method
        reg_obj = getattr(_reg, "registry", None)
        if reg_obj is not None:
            run_m = getattr(reg_obj, "run", None)
            if callable(run_m):
                return run_m, reg_obj
    except Exception:
        pass

    # 2) Try loader.get_registry()
    try:
        from v2.backend.core.spine import loader as _loader  # type: ignore
        get_reg = getattr(_loader, "get_registry", None)
        if callable(get_reg):
            reg = get_reg()
            run_m = getattr(reg, "run", None)
            if callable(run_m):
                return run_m, reg
    except Exception:
        pass

    # Not found
    return None, None


# ---------------------------- coercion utils ----------------------------

def _to_artifacts_list(res: Any) -> List[Any]:
    """
    Coerce any provider result into a list. The engine expects list-like returns.

    Accepted forms:
      - dict -> [dict]
      - object -> [object]
      - list/tuple -> list(res)
      - None -> []
    """
    if res is None:
        return []
    if isinstance(res, list):
        return res
    if isinstance(res, tuple):
        return list(res)
    # dict or object — wrap
    return [res]


# ------------------------------ API ------------------------------

def capability_run(name: str, payload: Dict[str, Any] | Any, context: Optional[Dict[str, Any]] = None) -> List[Any]:
    """
    Execute a Spine capability and normalize the result as a list.

    We try multiple calling conventions to be friendly with different
    Spine versions. The first that works is used.
    """
    runner, reg_ref = _resolve_runner()
    if runner is None:
        raise RuntimeError("Spine registry runner not available (could not resolve a run function)")

    # Try several invocation styles in order of verbosity.
    attempts = [
        # Common explicit signature
        lambda: runner(name, payload, context),
        # Some registries may ignore context
        lambda: runner(name, payload),
        # Keyword-only form
        lambda: runner(capability=name, payload=payload, context=context),
        # Some registries might use 'data' instead of 'payload'
        lambda: runner(name=name, data=payload, context=context),
    ]

    last_err: Optional[Exception] = None
    for attempt in attempts:
        try:
            res = attempt()
            return _to_artifacts_list(res)
        except TypeError as te:
            # Try next signature
            last_err = te
        except Exception as e:
            # Keep trying other signatures, but remember the last exception
            last_err = e

    # If we exhausted all attempts, surface the last error for visibility
    raise RuntimeError(f"Failed to invoke capability '{name}' via Spine runner") from last_err

