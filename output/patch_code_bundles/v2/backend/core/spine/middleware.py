# File: v2/backend/core/spine/middleware.py
from __future__ import annotations

"""
Built-in Spine middlewares.

Each middleware is a callable object with signature:

    mw(next_fn, capability: str, task: Task, context: Dict[str, Any]) -> List[Artifact]

- GuardMiddleware: basic payload sanity checks.
- TimingMiddleware: measures provider wall time and appends a timing artifact.
- RetriesMiddleware: retries on retryable Problem artifacts with simple backoff.
"""

import time
from typing import Any, Callable, Dict, List, Optional

from .contracts import Artifact, Task


MiddlewareFn = Callable[
    [Callable[[str, Task, Dict[str, Any]], List[Artifact]], str, Task, Dict[str, Any]],
    List[Artifact],
]


class GuardMiddleware:
    """Prevents obviously bad payloads from crossing the bus."""

    def __call__(
        self,
        next_fn: Callable[[str, Task, Dict[str, Any]], List[Artifact]],
        capability: str,
        task: Task,
        context: Dict[str, Any],
    ) -> List[Artifact]:
        payload = getattr(task, "payload", None)
        if payload is None:
            return [
                Artifact(
                    kind="Problem",
                    uri=f"spine://problem/{capability}",
                    sha256="",
                    meta={
                        "problem": {
                            "code": "InvalidPayload",
                            "message": "payload is None",
                            "retryable": False,
                            "details": {},
                        }
                    },
                )
            ]
        # Allow any JSON-serializable shape, but most providers expect dict.
        # If it's not a dict, we still let it pass; the provider may accept lists/strings.
        return next_fn(capability, task, context)


class TimingMiddleware:
    """Measures provider wall time and appends a small timing artifact."""

    def __init__(self, emit_artifact: bool = True) -> None:
        self.emit_artifact = emit_artifact

    def __call__(
        self,
        next_fn: Callable[[str, Task, Dict[str, Any]], List[Artifact]],
        capability: str,
        task: Task,
        context: Dict[str, Any],
    ) -> List[Artifact]:
        t0 = time.perf_counter()
        arts = next_fn(capability, task, context)
        dt = max(0.0, time.perf_counter() - t0)
        if self.emit_artifact:
            arts = arts + [
                Artifact(
                    kind="Trace",
                    uri=f"spine://trace/{capability}",
                    sha256="",
                    meta={
                        "timing_s": round(dt, 6),
                        "capability": capability,
                        "annotations": dict(getattr(task, "envelope", None).annotations or {}),
                    },
                )
            ]
        return arts


class RetriesMiddleware:
    """
    Retries provider calls when a retryable Problem artifact is returned.

    A Problem is considered retryable when meta.problem.retryable==True OR when
    meta.problem.code is in COMMON_TRANSIENTS.
    """

    COMMON_TRANSIENTS = {"RateLimit", "Timeout", "DeadlineExceeded", "ServiceUnavailable", "TooManyRequests"}

    def __init__(self, max_attempts: int = 2, backoff_s: float = 0.5) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_s = max(0.0, float(backoff_s))

    def __call__(
        self,
        next_fn: Callable[[str, Task, Dict[str, Any]], List[Artifact]],
        capability: str,
        task: Task,
        context: Dict[str, Any],
    ) -> List[Artifact]:
        attempt = 0
        while True:
            attempt += 1
            arts = next_fn(capability, task, context)
            retry = False
            for a in arts:
                if a.kind != "Problem":
                    continue
                meta = a.meta or {}
                prob = meta.get("problem") or {}
                if prob.get("retryable") is True or (prob.get("code") in self.COMMON_TRANSIENTS):
                    retry = True
                    break
            if not retry or attempt >= self.max_attempts:
                return arts
            time.sleep(self.backoff_s)



