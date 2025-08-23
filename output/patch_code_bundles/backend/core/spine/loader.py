# File: backend/core/spine/loader.py
from __future__ import annotations

"""
CapabilitiesLoader
------------------
Loads a YAML map of capability → provider target and registers them with the
Spine Registry.

YAML shape:

---
llm.engine.run.v1:
  target: backend.core.prompt_pipeline.executor.engine:run_v1
  input_schema: llm.engine.run.v1.input
  output_schema: llm.engine.run.v1.output

docstrings.scan.python.v1:
  target: backend.core.docstrings.providers:scan_python_v1
  input_schema: docstrings.scan.python.v1.input
  output_schema: docstrings.scan.python.v1.output

Notes:
- `target` must be the string "module.path:function_name".
- `input_schema` and `output_schema` are optional; if provided, the Registry’s
  validation layer may use them.
"""

from pathlib import Path
from typing import Any, Dict

import yaml

from .registry import Registry


class CapabilitiesLoader:
    def __init__(self, yaml_path: str | Path) -> None:
        self.path = Path(yaml_path).resolve()

    def load(self, registry: Registry) -> Registry:
        if not self.path.exists():
            raise FileNotFoundError(f"capabilities file not found: {self.path}")

        raw = self.path.read_text(encoding="utf-8")
        data: Dict[str, Dict[str, Any]] = yaml.safe_load(raw) or {}

        if not isinstance(data, dict):
            raise ValueError(f"capabilities YAML must be a mapping, got {type(data).__name__}")

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


__all__ = ["CapabilitiesLoader"]


# ---------------------------- static self-test ---------------------------------

if __name__ == "__main__":
    """
    Minimal static tests:
      1) Happy path: create a temp YAML that points to a local provider function,
         ensure it registers and dispatch works.
      2) Negative: invalid target format → raises ValueError.

    Exits non-zero on failure.
    """
    import json
    import tempfile
    from .contracts import new_envelope, Task
    from .validation import VALIDATORS

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
            len(arts) == 1
            and arts[0].kind == "Result"
            and isinstance(arts[0].meta, dict)
            and (arts[0].meta.get("result") or {}).get("ok") is True
            and (arts[0].meta.get("result") or {}).get("payload", {}).get("x") == 1
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



