# File: v2/backend/core/spine/loader.py
from __future__ import annotations

"""
Spine capability loader and thin functional API.

- CapabilitiesLoader: reads a YAML mapping of capability → provider target
  and registers them with the Registry instance passed in.

- Module-level singleton REGISTRY plus convenience functions:
    register(name, target, *, input_schema=None, output_schema=None)
    capability_run(name, payload, context=None) -> list[Artifact|dict]
    has(name) -> bool

This module is domain-agnostic. Provider targets are referenced by the YAML
(or direct calls to `register`) using "module.path:function_name" strings or
direct callables.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .registry import Registry


# ------------------------------------------------------------------------------
# YAML loader
# ------------------------------------------------------------------------------

class CapabilitiesLoader:
    """
    Load capability→provider mappings from a YAML file.

    YAML shape example:

        ---
        llm.engine.run.v1:
          target: v2.backend.core.prompt_pipeline.executor.engine:run_v1
          input_schema: llm.engine.run.v1.input
          output_schema: llm.engine.run.v1.output

        prompts.build.v1:
          target: v2.backend.core.prompt_pipeline.executor.providers:build_prompts_v1

    Notes:
      • 'target' must be "module.path:function".
      • input_schema and output_schema are optional and stored by Registry.
    """

    def __init__(self, yaml_path: str | Path) -> None:
        self.path = Path(yaml_path).resolve()

    def load(self, registry: Registry) -> Registry:
        if not self.path.exists():
            raise FileNotFoundError(f"capabilities file not found: {self.path}")

        raw = self.path.read_text(encoding="utf-8")
        data: Dict[str, Dict[str, Any]] = yaml.safe_load(raw) or {}

        if not isinstance(data, dict):
            raise ValueError(
                f"capabilities YAML must be a mapping, got {type(data).__name__}"
            )

        for capability, spec in data.items():
            if not isinstance(spec, dict):
                raise ValueError(
                    f"spec for {capability!r} must be a mapping, got {type(spec).__name__}"
                )

            target = spec.get("target")
            if not target or ":" not in str(target):
                raise ValueError(
                    f"Invalid target for {capability!r}: {target!r} (expected 'module:function')"
                )

            registry.register(
                capability=capability,
                target=str(target),
                input_schema=spec.get("input_schema"),
                output_schema=spec.get("output_schema"),
            )

        return registry


# ------------------------------------------------------------------------------
# Module-level singleton + thin functional API (generic, domain-agnostic)
# ------------------------------------------------------------------------------

_REGISTRY_SINGLETON = Registry()

def register(
    name: str,
    target: Any,
    *,
    input_schema: Optional[str] = None,
    output_schema: Optional[str] = None,
) -> None:
    """
    Register a capability implementation globally.
    `target` may be a callable or a 'module:function' string (lazy import).
    """
    _REGISTRY_SINGLETON.register(
        capability=name,
        target=target,
        input_schema=input_schema,
        output_schema=output_schema,
    )

def capability_run(name: str, payload: Dict[str, Any], context: Optional[Dict[str, Any]] = None):
    """
    Invoke a registered capability by name. Returns provider artifacts (list).
    """
    return _REGISTRY_SINGLETON.run(name, payload, context)

def has(name: str) -> bool:
    """Return True if a provider is registered for 'name'."""
    return _REGISTRY_SINGLETON.has(name)

# Optional: expose the singleton for advanced use
REGISTRY = _REGISTRY_SINGLETON


__all__ = ["CapabilitiesLoader", "register", "capability_run", "has", "REGISTRY"]


# ---------------------------- static self-test ---------------------------------

if __name__ == "__main__":
    """
    Minimal static tests:
      1) Happy path: create a temp YAML that points to a local provider function,
         ensure it registers and dispatch works via Registry.dispatch_task.
      2) Negative: invalid target format → raises ValueError.

    Exits non-zero on failure.
    """
    import tempfile
    from .contracts import new_envelope, Task  # type: ignore

    failures = 0

    # Local provider to be referenced by YAML using this module's name.
    def _echo_provider(task: Task, context: dict) -> dict:
        return {"ok": True, "payload": dict(task.payload or {}), "ctx": dict(context or {})}

    # 1) Happy path
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        yml = td_path / "caps.yml"
        yml.write_text(
            "\n".join(
                [
                    "unit.test.echo.v1:",
                    f"  target: {__name__}:_echo_provider",
                ]
            ),
            encoding="utf-8",
        )

        reg = Registry()
        CapabilitiesLoader(yml).load(reg)

        env = new_envelope(
            intent="test",
            subject="-",
            capability="unit.test.echo.v1",
            producer="selftest",
        )
        task = Task(envelope=env, payload_schema=None, payload={"x": 1})
        arts = reg.dispatch_task(task, context={"y": 2})

        ok = (
            isinstance(arts, list)
            and len(arts) == 1
            and getattr(arts[0], "kind", "") in ("Result", "Artifact", "ResultArtifact")
        )
        print("[loader.selftest] happy path:", "OK" if ok else "FAIL")
        failures += 0 if ok else 1

    # 2) Negative: invalid target format
    try:
        with tempfile.TemporaryDirectory() as td:
            yml2 = Path(td) / "bad.yml"
            yml2.write_text(
                "bad.cap:\n  target: not_a_module_and_no_colon\n", encoding="utf-8"
            )
            reg2 = Registry()
            CapabilitiesLoader(yml2).load(reg2)
        print("[loader.selftest] invalid target: FAIL")
        failures += 1
    except ValueError:
        print("[loader.selftest] invalid target: OK")

    raise SystemExit(failures)





