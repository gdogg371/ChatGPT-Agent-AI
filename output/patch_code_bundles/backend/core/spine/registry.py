# File: backend/core/spine/registry.py
from __future__ import annotations

"""
Spine Registry
==============

- Keeps a table of `capability -> provider target`.
- Builds a middleware chain and dispatches `Task` objects to providers.
- Normalizes provider outputs into a `List[Artifact]`.
- Runs input/output validators registered in `spine.validation`.

Provider target format:
    "module.path:function_name"

Provider call signature:
    def provider(task: Task, context: Dict[str, Any]) -> List[Artifact] | Artifact | Any

Normalization rules:
- If the provider returns a list of Artifact → returned as-is.
- If it returns a single Artifact → wrapped into a list.
- Otherwise (dict/list/primitive) → wrapped into a single
  Artifact(kind="Result", meta={"result": value}).

Middlewares:
- Functions of shape:
    mw(next_fn, capability: str, task: Task, context: Dict[str, Any]) -> List[Artifact]
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

    # ------------------------------------------------------------------ API

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
            return [
                self._problem(
                    capability=cap,
                    code="CapabilityUnavailable",
                    message=f"no provider for {cap!r}",
                )
            ]

        # -------- Input validation (if any)
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

            # -------- Output validation (if any) against the baton/result.
            if len(arts) == 1 and isinstance(arts[0].meta, dict) and "result" in arts[0].meta:
                baton: Any = arts[0].meta["result"]
            else:
                baton = [to_dict(a) for a in arts]

            try:
                VALIDATORS.validate_output(inner_capability, baton)
            except Exception as e:
                # attach a validation problem artifact but still return provider artifacts
                arts = arts + [
                    self._problem(
                        capability=inner_capability,
                        code="ValidationError",
                        message=str(e),
                        details={"phase": "output"},
                    )
                ]
            return arts

        # -------- Build middleware chain (last added is outermost)
        def _wrap_middleware(middleware: Middleware, next_fn: Callable[[str, Task, Dict[str, Any]], List[Artifact]]):
            def wrapper(capability: str, task_: Task, ctx_: Dict[str, Any]) -> List[Artifact]:
                return middleware(next_fn, capability, task_, ctx_)
            return wrapper

        chain: Callable[[str, Task, Dict[str, Any]], List[Artifact]] = call
        for middleware in reversed(self.middlewares):
            chain = _wrap_middleware(middleware, chain)

        # -------- Execute
        return chain(cap, task, dict(context or {}))

    # -------------------------------------------------------------- helpers

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


# ---------------------------- static self-test ---------------------------------

if __name__ == "__main__":
    """
    Minimal static tests (no YAML, no external providers):
      - register a local provider via target string f"{__name__}:_test_provider"
      - ensure middleware order & normalization
      - exercise input/output validation hooks
    Exits non-zero on failure.
    """
    from .contracts import Envelope, Task, new_envelope
    from .validation import VALIDATORS, require_fields

    failures = 0
    CAP = "unit.test.v1"

    # ---- Local provider target
    def _test_provider(task: Task, context: Dict[str, Any]) -> Dict[str, Any]:
        # Echo payload for inspection
        baton = dict(task.payload)
        if context.get("mw_flag"):
            baton["mw"] = True
        return {"ok": True, "baton": baton}

    # ---- Registry with a simple middleware
    reg = Registry()

    def mw_fn(next_fn, capability: str, task: Task, ctx: Dict[str, Any]) -> List[Artifact]:
        ctx = dict(ctx)
        ctx["mw_flag"] = True
        return next_fn(capability, task, ctx)

    reg.add_middleware(mw_fn)

    # ---- Validators
    VALIDATORS.register_input(CAP, require_fields(["x"]))   # require payload["x"]

    # ---- Register provider
    reg.register(capability=CAP, target=f"{__name__}:_test_provider")

    # ---- Build a task and dispatch
    env: Envelope = new_envelope(intent="test", subject="-", capability=CAP, producer="selftest")
    task = Task(envelope=env, payload_schema=CAP, payload={"x": 1})

    arts = reg.dispatch_task(task, context={})
    ok = len(arts) == 1 and arts[0].kind == "Result" and (arts[0].meta or {}).get("result", {}).get("baton", {}).get("mw") is True
    print("[registry.selftest] dispatch:", "OK" if ok else "FAIL")
    failures += 0 if ok else 1

    # ---- Negative: missing required field
    bad_task = Task(envelope=env, payload_schema=CAP, payload={})
    bad = reg.dispatch_task(bad_task, context={})
    ok2 = len(bad) == 1 and bad[0].kind == "Problem" and (bad[0].meta or {}).get("problem", {}).get("code") == "ValidationError"
    print("[registry.selftest] input validation:", "OK" if ok2 else "FAIL")
    failures += 0 if ok2 else 1

    raise SystemExit(failures)





