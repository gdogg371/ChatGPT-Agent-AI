# File: v2/backend/core/spine/registry.py
"""
Spine Capability Registry (runtime)

- Maps capability name → callable (provider function).
- Stable `.run(name, payload, context)` API used by orchestrator/loader.
- Normalizes provider returns into lightweight Artifact records.
- **Promotes plain-dict errors** (e.g., {"error": "...", ...}) to Problem artifacts
  so engines always see failures without guessing.

Exports (compat):
- class CapabilityRegistry   (primary)
- class Registry             (alias of CapabilityRegistry)
- REGISTRY                   (process-wide singleton)
- registry                   (alias of REGISTRY)
- run(), run_capability()    (module-level proxies)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
import hashlib
import importlib
import inspect
import json
import traceback


# ------------------------------- Artifacts ----------------------------------


@dataclass
class Artifact:
    """Minimal artifact envelope for capability outputs."""
    kind: str                  # "Result" | "Problem" | ...
    uri: str                   # e.g., spine://result/<cap> or spine://capability/<cap>
    sha256: str                # deterministic content hash (may be empty for Problems)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "uri": self.uri, "sha256": self.sha256, "meta": self.meta}


def _sha256(kind: str, uri: str, meta: Dict[str, Any]) -> str:
    """Compute a deterministic sha256 over a small JSON envelope."""
    def _jsonable(x: Any) -> Any:
        try:
            json.dumps(x)
            return x
        except Exception:
            return repr(x)

    blob = json.dumps(
        {"kind": kind, "uri": uri, "meta": _jsonable(meta or {})},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        default=repr,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _make_result(cap: str, meta: Dict[str, Any]) -> Artifact:
    uri = f"spine://result/{cap}"
    return Artifact("Result", uri, _sha256("Result", uri, meta), meta)


def _make_problem(cap: str, code: str, message: str, *, details: Optional[Dict[str, Any]] = None,
                  exc: BaseException | None = None, extra_meta: Optional[Dict[str, Any]] = None) -> Artifact:
    """
    Compose a Problem artifact with both a structured `problem` block and flat error/message
    fields for legacy collectors. Includes traceback if `exc` is provided.
    """
    meta: Dict[str, Any] = {
        "problem": {"code": code, "message": message, "retryable": False, "details": details or {}},
        "error": code,
        "message": message,
    }
    if exc is not None:
        meta["exception"] = repr(exc)
        meta["traceback"] = traceback.format_exc()
    if extra_meta:
        # merge without clobbering the 'problem' keys
        for k, v in extra_meta.items():
            if k == "problem":
                continue
            meta[k] = v

    uri = f"spine://capability/{cap}"
    return Artifact("Problem", uri, _sha256("Problem", uri, meta), meta)


# ---------------------------- Capability Registry ---------------------------


class CapabilityRegistry:
    """In-memory map of capabilities to provider callables."""

    def __init__(self) -> None:
        # capability -> spec dict: {"target": callable|str, "input_schema": str|None, "output_schema": str|None}
        self._caps: Dict[str, Dict[str, Any]] = {}

    # ---- registration ----

    def register(
        self,
        capability: str,
        target: Any,
        input_schema: Optional[str] = None,
        output_schema: Optional[str] = None,
    ) -> None:
        if not isinstance(capability, str) or not capability:
            raise ValueError("Capability name must be a non-empty string")
        self._caps[capability] = {
            "target": target,
            "input_schema": input_schema,
            "output_schema": output_schema,
        }

    def bulk_register(self, mapping: Dict[str, Any]) -> None:
        for k, spec in (mapping or {}).items():
            if isinstance(spec, dict):
                self.register(k, spec.get("target"), spec.get("input_schema"), spec.get("output_schema"))
            else:
                self.register(k, spec)

    # ---- lookup ----

    def _resolve(self, capability: str) -> Callable[..., Any]:
        if capability not in self._caps:
            raise KeyError(f"No provider registered for '{capability}'")
        target = self._caps[capability]["target"]
        if callable(target):
            return target
        if isinstance(target, str) and ":" in target:
            mod, fn = target.split(":", 1)
            func = getattr(importlib.import_module(mod), fn)
            if not callable(func):
                raise TypeError(f"Target for {capability} is not callable: {target!r}")
            # cache resolved callable for future
            self._caps[capability]["target"] = func
            return func
        raise TypeError(f"Invalid target spec for {capability}: {target!r}")

    def has(self, capability: str) -> bool:
        return capability in self._caps

    def names(self) -> List[str]:
        return sorted(self._caps.keys())

    # ---- normalization (with error promotion) ----

    def _normalize_to_artifacts(self, capability: str, obj: Any) -> List[Artifact]:
        """
        Convert any provider return into a list[Artifact], *promoting* plain-dict errors
        (presence of an 'error' key) to Problem artifacts.
        """
        if obj is None:
            return []

        # List-like
        if isinstance(obj, list):
            out: List[Artifact] = []
            for i, x in enumerate(obj):
                # Already an Artifact?
                if isinstance(x, Artifact):
                    if not getattr(x, "sha256", None):
                        x.sha256 = _sha256(x.kind, x.uri, x.meta or {})
                    out.append(x)
                    continue

                # Artifact-like dict?
                if isinstance(x, dict) and {"kind", "uri", "meta"} <= set(x.keys()):
                    kind = x.get("kind")
                    uri = x.get("uri")
                    meta = x.get("meta") or {}
                    sha = x.get("sha256") or _sha256(kind, uri, meta)
                    out.append(Artifact(kind, uri, sha, meta))
                    continue

                # Plain dict → promote error if present else wrap as Result
                if isinstance(x, dict):
                    if "error" in x:
                        err = x.get("error")
                        msg = str(err)
                        details = {k: v for k, v in x.items() if k != "error"}
                        out.append(_make_problem(capability, "ProviderError", msg, details=details, extra_meta=x))
                    else:
                        out.append(_make_result(capability, {"result": x, "index": i}))
                    continue

                # Fallback: non-dict → repr
                out.append(_make_result(capability, {"result": repr(x), "index": i, "note": "non-dict coerced via repr"}))
            return out

        # Dict-like
        if isinstance(obj, dict):
            # Artifact-like dict?
            if {"kind", "uri", "meta"} <= set(obj.keys()):
                kind = obj.get("kind")
                uri = obj.get("uri")
                meta = obj.get("meta") or {}
                sha = obj.get("sha256") or _sha256(kind, uri, meta)
                return [Artifact(kind, uri, sha, meta)]  # type: ignore[arg-type]

            # Plain dict → promote error if present
            if "error" in obj:
                err = obj.get("error")
                msg = str(err)
                details = {k: v for k, v in obj.items() if k != "error"}
                return [_make_problem(capability, "ProviderError", msg, details=details, extra_meta=obj)]

            # Success dict
            return [_make_result(capability, {"result": obj})]

        # Fallback: any other type
        return [_make_result(capability, {"result": repr(obj), "note": "non-dict coerced via repr"})]

    # ---- execution (signature-aware) ----

    def run(self, capability: str, payload: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> List[Artifact]:
        """
        Execute a capability and wrap provider results in Artifacts.

        Signature-aware dispatch:
          - If provider params start with ('name'|'capability'|'cap'), pass the capability as first arg.
          - If a param looks like ('task'|'task_like'), pass a wrapper with `.payload`.
          - Otherwise, pass the raw payload dict.
          - Supports context positional or keyword; falls back to tolerant variants.
        """
        try:
            fn = self._resolve(capability)
        except Exception as e:
            return [ _make_problem(capability, "CapabilityNotFound", str(e), exc=e) ]

        ctx = context or {}

        class _TaskLike:
            __slots__ = ("payload",)

            def __init__(self, p: Any) -> None:
                self.payload = p

        wrapped = _TaskLike(payload)

        # Inspect parameters to choose the safest call form.
        names: List[str] = []
        try:
            sig = inspect.signature(fn)
            params = list(sig.parameters.values())
            names = [p.name for p in params]
        except Exception:
            names = []

        def _call_with(*args, **kwargs):
            return fn(*args, **kwargs)

        # 1) Signature-based best guess
        try:
            if names:
                first = names[0]
                second = names[1] if len(names) > 1 else None
                third = names[2] if len(names) > 2 else None

                # run(name, payload|task, context?)
                if first in ("name", "capability", "cap") and second is not None:
                    wants_task = second in ("task", "task_like")
                    arg2 = wrapped if wants_task else payload
                    if third is not None:
                        return self._normalize_to_artifacts(capability, _call_with(capability, arg2, ctx))
                    return self._normalize_to_artifacts(capability, _call_with(capability, arg2))

                # run(task, context?) / run(task)
                if first in ("task", "task_like"):
                    if second is not None:
                        return self._normalize_to_artifacts(capability, _call_with(wrapped, ctx))
                    return self._normalize_to_artifacts(capability, _call_with(wrapped))

                # run(payload, context?) / run(payload)
                if first in ("payload", "data", "params", "spec"):
                    if second is not None:
                        return self._normalize_to_artifacts(capability, _call_with(payload, ctx))
                    return self._normalize_to_artifacts(capability, _call_with(payload))
        except TypeError:
            # Fall through to tolerant ladder
            pass
        except Exception as e:
            return [ _make_problem(capability, "ProviderError", f"error during signature-based dispatch: {e}", exc=e) ]

        # 2) Tolerant ladder (covers odd signatures)
        attempts = [
            lambda: _call_with(wrapped, ctx),
            lambda: _call_with(wrapped),
            lambda: _call_with(payload, ctx),
            lambda: _call_with(payload),
            lambda: _call_with(task_like=wrapped, context=ctx),
            lambda: _call_with(task_like=wrapped),
        ]

        last_type_error: Optional[BaseException] = None

        for idx, call in enumerate(attempts):
            try:
                return self._normalize_to_artifacts(capability, call())
            except TypeError as te:
                last_type_error = te
                continue
            except AttributeError as ae:
                msg = str(ae)
                # If we passed a raw dict and provider expected `.payload`, retry with wrapped once.
                if idx == 3 and ("has no attribute 'payload'" in msg or "object has no attribute 'payload'" in msg):
                    try:
                        return self._normalize_to_artifacts(capability, _call_with(wrapped))
                    except Exception as e_wrap:
                        return [ _make_problem(capability, "ProviderError", f"after task-like retry: {e_wrap}", exc=e_wrap) ]
                return [ _make_problem(capability, "ProviderError", f"{ae}", exc=ae) ]
            except Exception as e:
                return [ _make_problem(capability, "ProviderError", f"{e}", exc=e) ]

        # 3) Exhausted only with TypeErrors
        return [
            _make_problem(
                capability,
                "ProviderInvocationError",
                f"incompatible runner signature (last TypeError: {last_type_error})",
                exc=last_type_error if isinstance(last_type_error, BaseException) else None,
            )
        ]


# Back-compat class name expected by some imports
Registry = CapabilityRegistry

# Process-wide singleton exports (expected by spine.__init__ and loader)
REGISTRY = CapabilityRegistry()
registry = REGISTRY


# Module-level runner proxies (expected by spine.__init__)
def run(capability: str, payload: Any, context: Optional[Dict[str, Any]] = None):
    return REGISTRY.run(capability, payload, context)


def run_capability(name: str, payload: Any, context: Optional[Dict[str, Any]] = None):
    return REGISTRY.run(name, payload, context)










