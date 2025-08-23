# File: v2/backend/core/spine/bootstrap.py
from __future__ import annotations

"""
Spine bootstrap & high-level API.

- Encapsulates a Registry
- Installs default middlewares
- Loads capability map from YAML
- Provides dispatch helpers and a tiny declarative pipeline runner
"""

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

from .contracts import Artifact, Task, new_envelope, to_dict
from .loader import CapabilitiesLoader
from .middleware import GuardMiddleware, TimingMiddleware, RetriesMiddleware
from .registry import Registry


class Spine:
    """
    Class-based spine entrypoint. Encapsulates a Registry instance,
    middleware setup, capability loading, and pipeline execution.
    """

    def __init__(
        self,
        caps_path: str | Path,
        *,
        middlewares: Optional[List] = None,
        load_caps: bool = True,
    ) -> None:
        self.caps_path = Path(caps_path).resolve()
        self.registry = Registry()

        # Cross-cutting concerns (order matters: last added is outermost)
        if middlewares is None:
            middlewares = [GuardMiddleware(), TimingMiddleware(), RetriesMiddleware(max_attempts=2)]
        for mw in middlewares:
            # accept instances or callables
            self.registry.add_middleware(mw)  # type: ignore[arg-type]

        if load_caps:
            CapabilitiesLoader(self.caps_path).load(self.registry)

    # ---- Dispatch -------------------------------------------------------------

    def dispatch_task(self, task: Task, *, context: Optional[Dict[str, Any]] = None) -> List[Artifact]:
        """Dispatch a fully-formed Task to the registered provider chain."""
        return self.registry.dispatch_task(task, context=context or {})

    def dispatch_capability(
        self,
        *,
        capability: str,
        payload: Dict[str, Any],
        intent: str = "pipeline",
        subject: str = "-",
        producer: str = "spine",
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Artifact]:
        """
        Convenience: build Envelope+Task and dispatch.
        """
        env = new_envelope(intent=intent, subject=subject, capability=capability, producer=producer)
        task = Task(envelope=env, payload_schema=capability, payload=payload)
        return self.dispatch_task(task, context=context or {})

    # ---- Pipelines ------------------------------------------------------------

    @staticmethod
    def _extract_prev_result(arts: List[Artifact]) -> Any:
        """
        Baton rule:
          - If exactly one Artifact has meta['result'], pass that.
          - Else pass a JSON-serializable list of all artifacts.
        """
        if len(arts) == 1 and isinstance(arts[0].meta, dict) and "result" in arts[0].meta:
            return arts[0].meta["result"]
        return [to_dict(a) for a in arts]

    def run_pipeline(
        self,
        pipeline_steps: Iterable[dict],
        *,
        variables: Optional[Dict[str, Any]] = None,
    ) -> List[Artifact]:
        """
        Execute a declarative pipeline where each step is:
          { call: <capability>, with: { ...payload fields... } }

        Special token:
          - $prev → replaced with previous step’s baton.
        """
        variables = dict(variables or {})
        prev_baton: Any = None
        last_arts: List[Artifact] = []

        for i, step in enumerate(pipeline_steps):
            if not isinstance(step, dict):
                raise TypeError(f"pipeline step #{i} must be a mapping, got {type(step).__name__}")
            cap = step.get("call")
            if not cap:
                raise ValueError(f"pipeline step #{i} missing 'call'")
            args = step.get("with", {}) or {}
            if not isinstance(args, dict):
                raise TypeError(f"pipeline step #{i} 'with' must be a mapping")

            def resolve(v):
                # Variable interpolation: ${VAR_NAME} → variables["VAR_NAME"]
                if isinstance(v, str):
                    if v == "$prev":
                        return prev_baton
                    if v.startswith("${") and v.endswith("}"):
                        key = v[2:-1]
                        return variables.get(key, v)
                return v

            payload = {k: resolve(v) for k, v in args.items()}

            arts = self.dispatch_capability(
                capability=cap,
                payload=payload,
                intent="pipeline",
                subject=f"step://{i}/{cap}",
                context={"step_index": i, **variables},
            )
            last_arts = arts

            # Early exit if any artifact encodes a problem.
            if any(a.kind == "Problem" for a in arts):
                return last_arts

            prev_baton = self._extract_prev_result(arts)

        return last_arts

    def load_pipeline_and_run(
        self,
        pipeline_yaml: str | Path,
        *,
        variables: Optional[Dict[str, Any]] = None,
    ) -> List[Artifact]:
        """
        Load a YAML pipeline file (list of steps) and execute it.
        """
        p = Path(pipeline_yaml).resolve()
        steps = yaml.safe_load(p.read_text(encoding="utf-8")) or []
        if not isinstance(steps, list):
            raise TypeError(f"pipeline YAML must contain a list of steps: {p}")
        return self.run_pipeline(steps, variables=variables or {})



