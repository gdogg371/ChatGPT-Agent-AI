# File: v2/backend/core/spine/loader.py
from __future__ import annotations

"""
CapabilitiesLoader
------------------
Loads a YAML map of capability → provider target and registers them
with the Spine Registry.

YAML example:

---
llm.engine.run.v1:
  target: v2.backend.core.prompt_pipeline.executor.engine:run_v1
  input_schema: llm.engine.run.v1.input
  output_schema: llm.engine.run.v1.output

docstrings.scan.python.v1:
  target: v2.backend.core.docstrings.providers:scan_python_v1
  input_schema: docstrings.scan.python.v1.input
  output_schema: docstrings.scan.python.v1.output
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

        data: Dict[str, Dict[str, Any]] = yaml.safe_load(
            self.path.read_text(encoding="utf-8")
        ) or {}

        if not isinstance(data, dict):
            raise ValueError(f"capabilities YAML must be a mapping, got {type(data).__name__}")

        for capability, spec in data.items():
            if not isinstance(spec, dict):
                raise ValueError(f"spec for {capability!r} must be a mapping, got {type(spec).__name__}")
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


