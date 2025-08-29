# File: v2/backend/core/spine/registry.py
"""
Spine Capability Registry (runtime)

- Maps capability name → callable (provider function).
- Stable `.run(name, payload, context)` API used by orchestrator/loader.
- Wraps provider results in lightweight Artifact records for traceability.

Compat:
- Exposes `REGISTRY` and `registry` (aliases) plus module-level `run`/`run_capability`.
- Normalizes outputs to a list[Artifact].
- Computes a deterministic sha256 for each Artifact if not supplied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
import hashlib
import json
import traceback


# ------------------------------- Artifacts ----------------------------------


@dataclass
class Artifact:
    """Minimal artifact envelope for capability outputs."""
    kind: str
    uri: str
    sha256: str
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "uri": self.uri, "sha256": self.sha256, "meta": self.meta}


def _compute_sha256_from_parts(kind: str, uri: str, meta: Dict[str, Any]) -> str:
    def _jsonable(x: Any) -> Any:
        try:
            json.dumps(x)
            return x
        except Exception:
            return repr(x)

    safe_meta = _jsonable(meta or {})
    blob = json.dumps(
        {"kind": kind, "uri": uri, "meta": safe_meta},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        default=repr,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _make_artifact(kind: str, uri: str, meta: Dict[str, Any]) -> Artifact:
    sha = _compute_sha256_from_parts(kind, uri, meta or {})
    return Artifact(kind=kind, uri=uri, sha256=sha, meta=meta or {})


# ---------------------------- Capability Registry ---------------------------


class CapabilityRegistry:
    """In-memory map of capabilities to provider callables."""

    def __init__(self) -> None:
        self._caps: Dict[str, Callable[..., Any]] = {}

    # ---- registration ----

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("Capability name must be a non-empty string")
        if not callable(fn):
            raise TypeError("Capability provider must be callable")
        self._caps[name] = fn

    def bulk_register(self, mapping: Dict[str, Callable[..., Any]]) -> None:
        for k, v in (mapping or {}).items():
            self.register(k, v)

    # ---- lookup ----

    def get(self, name: str) -> Optional[Callable[..., Any]]:
        return self._caps.get(name)

    def names(self) -> List[str]:
        return sorted(self._caps.keys())

    # ---- execution ----

    def run(self, capability: str, payload: Any, context: Optional[Dict[str, Any]] = None) -> List[Artifact]:
        """
        Execute a capability and wrap provider results in Artifacts.

        Robust calling order to satisfy providers with different signatures:
          1) fn(TaskLike(payload), context)
          2) fn(TaskLike(payload))
          3) fn(payload, context)
          4) fn(payload)              [with retry-on-'.payload' to (2)]
          5) fn(task_like=TaskLike(payload), context=context)
          6) fn(task_like=TaskLike(payload))

        Any real provider exception is surfaced as a Problem artifact with traceback.
        """
        fn = self.get(capability)
        if fn is None:
            return [
                _make_artifact(
                    kind="Problem",
                    uri=f"spine://capability/{capability}",
                    meta={"error": "CapabilityNotFound", "message": f"No provider registered for '{capability}'"},
                )
            ]

        # Local helper: wrap dict payloads for providers that expect `.payload`
        class _TaskLike:
            __slots__ = ("payload",)

            def __init__(self, p: Any) -> None:
                self.payload = p

        wrapped = _TaskLike(payload)
        ctx = context or {}

        # Candidate invocations in strict order
        attempts: List[Callable[[], Any]] = [
            lambda: fn(wrapped, ctx),                             # 1
            lambda: fn(wrapped),                                  # 2
            lambda: fn(payload, ctx),                             # 3
            lambda: fn(payload),                                  # 4  (may raise '.payload' AttributeError)
            lambda: fn(task_like=wrapped, context=ctx),           # 5
            lambda: fn(task_like=wrapped),                        # 6
        ]

        last_type_error: Optional[BaseException] = None

        for i, attempt in enumerate(attempts):
            try:
                result = attempt()
                break
            except TypeError as te:
                # Signature mismatch for this variant — try the next.
                last_type_error = te
                continue
            except AttributeError as ae:
                # Common case: provider expected task-like with `.payload`, but we passed raw dict (variant 4).
                msg = str(ae)
                if i == 3 and ("has no attribute 'payload'" in msg or "object has no attribute 'payload'" in msg):
                    # Retry immediately with the single-arg wrapped form (variant 2) if not already tried.
                    try:
                        result = fn(wrapped)
                        break
                    except Exception as e_wrap:
                        return [
                            _make_artifact(
                                kind="Problem",
                                uri=f"spine://capability/{capability}",
                                meta={
                                    "error": "ProviderRuntimeError",
                                    "message": f"provider raised during '{capability}' (after task-like retry)",
                                    "exception": repr(e_wrap),
                                    "traceback": traceback.format_exc(),
                                },
                            )
                        ]
                # Any other AttributeError is a real provider error.
                return [
                    _make_artifact(
                        kind="Problem",
                        uri=f"spine://capability/{capability}",
                        meta={
                            "error": "ProviderRuntimeError",
                            "message": f"provider raised during '{capability}'",
                            "exception": repr(ae),
                            "traceback": traceback.format_exc(),
                        },
                    )
                ]
            except Exception as e:
                # Real provider error — surface it immediately.
                return [
                    _make_artifact(
                        kind="Problem",
                        uri=f"spine://capability/{capability}",
                        meta={
                            "error": "ProviderRuntimeError",
                            "message": f"provider raised during '{capability}'",
                            "exception": repr(e),
                            "traceback": traceback.format_exc(),
                        },
                    )
                ]
        else:
            # Exhausted all attempts with only TypeErrors
            return [
                _make_artifact(
                    kind="Problem",
                    uri=f"spine://capability/{capability}",
                    meta={
                        "error": "ProviderInvocationError",
                        "message": f"incompatible runner signature for '{capability}'",
                        "exception": repr(last_type_error),
                        "traceback": traceback.format_exc(),
                    },
                )
            ]

        # Normalize outputs into artifacts
        def _as_artifacts(obj: Any) -> List[Artifact]:
            if obj is None:
                return []
            if isinstance(obj, list):
                out: List[Artifact] = []
                for i, x in enumerate(obj):
                    if isinstance(x, Artifact):
                        if not getattr(x, "sha256", None):
                            x.sha256 = _compute_sha256_from_parts(x.kind, x.uri, x.meta or {})
                        out.append(x)
                    elif isinstance(x, dict):
                        out.append(
                            _make_artifact(
                                kind="Result", uri=f"spine://result/{capability}", meta={"result": x, "index": i}
                            )
                        )
                    else:
                        out.append(
                            _make_artifact(
                                kind="Result",
                                uri=f"spine://result/{capability}",
                                meta={"result": repr(x), "index": i, "note": "non-dict coerced via repr"},
                            )
                        )
                return out
            if isinstance(obj, Artifact):
                if not getattr(obj, "sha256", None):
                    obj.sha256 = _compute_sha256_from_parts(obj.kind, obj.uri, obj.meta or {})
                return [obj]
            if isinstance(obj, dict):
                return [_make_artifact(kind="Result", uri=f"spine://result/{capability}", meta={"result": obj})]
            return [
                _make_artifact(
                    kind="Result",
                    uri=f"spine://result/{capability}",
                    meta={"result": repr(obj), "note": "non-dict coerced via repr"},
                )
            ]

        return _as_artifacts(result)


# ------------------------------ module API ----------------------------------


REGISTRY = CapabilityRegistry()
registry = REGISTRY  # alias


def run(capability: str, payload: Any, context: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    """Module-level runner proxy for convenience/back-compat."""
    return REGISTRY.run(capability, payload, context)


def run_capability(name: str, payload: Any, context: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    """Legacy alias some callers import."""
    return REGISTRY.run(name, payload, context)




