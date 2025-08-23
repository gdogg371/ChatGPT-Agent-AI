# File: v2/backend/core/spine/registry.py
from __future__ import annotations

"""
Spine Registry
==============

- Keeps a table of `capability -> provider target`.
- Builds a middleware chain and dispatches `Task` objects to providers.
- Normalizes provider outputs into a `List[Artifact]`.
- Runs input/output validators registered in `spine.validation`.

Provider target format: "module.path:function_name"

Provider call signature:
    def provider(task: Task, context: Dict[str, Any]) -> List[Artifact] | Artifact | Any

Normalization rules:
- If the provider returns a list of Artifact → returned as-is.
- If it returns a single Artifact → wrapped into a list.
- Otherwise (dict/list/primitive) → wrapped into a single Artifact(kind="Result", meta={"result": value}).

Errors:
- Exceptions are caught and returned as a single Artifact(kind="Problem", meta={"problem": {...}}).

Middlewares:
- Functions of shape mw(next_fn, capability: str, task: Task, context: Dict[str, Any]) -> List[Artifact]
- Added via `add_middleware`. Last added is outermost (standard stacking).
"""

from dataclasses import dataclass
from importlib import import_module
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .contracts import Artifact, Problem, Task, to_dict
from .validation import VALIDATORS


ProviderFn = Callable[[Task, Dict[str, Any]], Union[List[Artifact], Artifact, Any]]
Middleware = Callable[
    [Callable[[str, Task, Dict[str, Any]], List[Artifact]], str, Task, Dict[str, Any]],
    List[Artifact],
]


@dataclass(frozen=True)
class _Provider:
    capability: str
    target: str
    fn: ProviderFn
    input_schema: Optional[str]
    output_schema: Optional[str]


class Registry:
    def __init__(self) -> None:
        self._providers: Dict[str, _Provider] = {}
        self.middlewares: List[Middleware] = []

    # --------------------------------------------------------------------- API

    def add_middleware(self, mw: Middleware) -> None:
        if not callable(mw):
            raise TypeError("middleware must be callable")
        self.middlewares.append(mw)

    def register(
        self,
        *,
        capability: str,
        target: str,
        input_schema: Optional[str] = None,
        output_schema: Optional[str] = None,
    ) -> None:
        """
        Register a capability → provider mapping.

        `target` must be "module.path:function".
        """
        mod_name, func_name = self._split_target(target)
        mod = import_module(mod_name)
        fn = getattr(mod, func_name, None)
        if not callable(fn):
            raise ValueError(f"target is not callable: {target}")

        self._providers[capability] = _Provider(
            capability=capability,
            target=target,
            fn=fn,  # type: ignore[assignment]
            input_schema=input_schema,
            output_schema=output_schema,
        )

    def dispatch_task(self, task: Task, *, context: Dict[str, Any]) -> List[Artifact]:
        """
        Dispatch a Task to the matching provider via middleware chain.
        """
        cap = task.envelope.capability
        prov = self._providers.get(cap)
        if prov is None:
            return [self._problem(capability=cap, code="CapabilityUnavailable", message=f"no provider for {cap!r}")]

        # Input validation (if any)
        try:
            VALIDATORS.validate_input(cap, task.payload)
        except Exception as e:
            return [
                self._problem(
                    capability=cap,
                    code="ValidationError",
                    message=str(e),
                    details={"phase": "input"},
                )
            ]

        def call(inner_capability: str, inner_task: Task, inner_ctx: Dict[str, Any]) -> List[Artifact]:
            try:
                raw = prov.fn(inner_task, inner_ctx)  # provider call
                arts = self._normalize_output(raw)
            except Exception as e:
                arts = [
                    self._problem(
                        capability=inner_capability,
                        code=type(e).__name__,
                        message=str(e),
                        details={"target": prov.target},
                    )
                ]

            # Output validation (if any) against the baton/result (meta["result"] when present)
            baton: Any = None
            if len(arts) == 1 and isinstance(arts[0].meta, dict) and "result" in arts[0].meta:
                baton = arts[0].meta["result"]
            else:
                baton = [to_dict(a) for a in arts]

            try:
                VALIDATORS.validate_output(inner_capability, baton)
            except Exception as e:
                # attach a validation problem artifact but still return the provider artifacts
                arts = arts + [
                    self._problem(
                        capability=inner_capability,
                        code="ValidationError",
                        message=str(e),
                        details={"phase": "output"},
                    )
                ]
            return arts

        # Build middleware chain (last added is outermost)
        chain: Callable[[str, Task, Dict[str, Any]], List[Artifact]] = call
        for mw in reversed(self.middlewares):
            prev = chain
            chain = (lambda cap_, task_, ctx_, mw=mw, prev=prev: mw(prev, cap_, task_, ctx_))

        # Execute
        return chain(cap, task, dict(context or {}))

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _split_target(target: str) -> Tuple[str, str]:
        if ":" not in target:
            raise ValueError(f"invalid target (expected 'module:function'): {target!r}")
        mod, fn = target.split(":", 1)
        return mod.strip(), fn.strip()

    @staticmethod
    def _normalize_output(raw: Union[List[Artifact], Artifact, Any]) -> List[Artifact]:
        # Already a list of Artifacts
        if isinstance(raw, list) and all(isinstance(x, Artifact) for x in raw):
            return raw  # type: ignore[return-value]
        # Single artifact
        if isinstance(raw, Artifact):
            return [raw]
        # Wrap arbitrary payload as Result artifact
        return [
            Artifact(
                kind="Result",
                uri="spine://result",
                sha256="",
                meta={"result": raw},
            )
        ]

    @staticmethod
    def _problem(
        *,
        capability: str,
        code: str,
        message: str,
        retryable: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ) -> Artifact:
        prob = Problem(code=code, message=message, retryable=retryable, details=dict(details or {}))
        return Artifact(
            kind="Problem",
            uri=f"spine://problem/{capability}",
            sha256="",
            meta={
                "problem": {
                    "code": prob.code,
                    "message": prob.message,
                    "retryable": prob.retryable,
                    "details": prob.details,
                }
            },
        )



