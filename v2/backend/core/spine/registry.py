# v2/backend/core/spine/registry.py
from __future__ import annotations

import importlib
from typing import Any, Callable, Dict, List, Optional, Tuple

# Try to use the real Artifact; fall back to a tiny shim to avoid import cycles.
try:
    from .contracts import Artifact  # type: ignore
except Exception:  # pragma: no cover
    class Artifact:  # type: ignore
        def __init__(self, kind: str, uri: str, sha256: str = "", meta: Optional[Dict[str, Any]] = None) -> None:
            self.kind = kind
            self.uri = uri
            self.sha256 = sha256
            self.meta = meta or {}

__all__ = ["Registry"]

class Registry:
    """
    Minimal, generic capability registry.

    Loader expectation:
      - register(capability=..., target=..., input_schema=..., output_schema=...)
      - dispatch_task(task, context)
    Engine expectation:
      - run(name, payload, context)
    """

    def __init__(self) -> None:
        # capability -> spec dict: {"target": <callable|str>, "input_schema": str|None, "output_schema": str|None}
        self._caps: Dict[str, Dict[str, Any]] = {}

    # ---------- registration ----------
    def register(
        self,
        capability: str,
        target: Any,
        input_schema: Optional[str] = None,
        output_schema: Optional[str] = None,
    ) -> None:
        self._caps[capability] = {
            "target": target,
            "input_schema": input_schema,
            "output_schema": output_schema,
        }

    # ---------- resolution ----------
    def _resolve(self, capability: str) -> Callable[..., Any]:
        if capability not in self._caps:
            raise KeyError(f"No provider registered for '{capability}'")
        target = self._caps[capability]["target"]
        if callable(target):
            return target
        if isinstance(target, str) and ":" in target:
            mod, func = target.split(":", 1)
            fn = getattr(importlib.import_module(mod), func)
            if not callable(fn):
                raise TypeError(f"Resolved {capability} -> {mod}:{func}, but it is not callable")
            return fn
        raise TypeError(f"Bad target for {capability}: {target!r}")

    # ---------- invocation (engine-style) ----------
    def run(self, capability: str, payload: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> List[Any]:
        fn = self._resolve(capability)
        try:
            result = fn(payload, context or {})
        except Exception as e:
            return [Artifact(kind="Problem", uri=f"spine://capability/{capability}", meta={
                "problem": {"code": "ProviderError", "message": str(e), "retryable": False, "details": {}}
            })]
        # If provider already returns artifacts, pass through
        if isinstance(result, list) and result and hasattr(result[0], "kind"):
            return result
        # Wrap plain payload into a Result artifact
        return [Artifact(kind="Result", uri=f"spine://result/{capability}", meta={"result": result})]

    # ---------- invocation (loader/self-test style) ----------
    def dispatch_task(self, task: Any, context: Optional[Dict[str, Any]] = None) -> List[Any]:
        """
        Expects a Task-like object with task.envelope.capability and task.payload.
        Used by loader.py self-test.
        """
        # Extract capability and payload from the Task-like object
        env = getattr(task, "envelope", None)
        capability = getattr(env, "capability", None)
        if not isinstance(capability, str) or not capability:
            raise ValueError("Task envelope is missing 'capability'")

        payload = getattr(task, "payload", None)
        if payload is None:
            payload = {}

        fn = self._resolve(capability)
        try:
            result = fn(task, context or {})
        except Exception as e:
            return [Artifact(kind="Problem", uri=f"spine://capability/{capability}", meta={
                "problem": {"code": "ProviderError", "message": str(e), "retryable": False, "details": {}}
            })]
        if isinstance(result, list) and result and hasattr(result[0], "kind"):
            return result
        return [Artifact(kind="Result", uri=f"spine://result/{capability}", meta={"result": result})]

    # ---------- utils ----------
    def names(self) -> List[str]:
        return list(self._caps.keys())

    def has(self, capability: str) -> bool:
        return capability in self._caps


