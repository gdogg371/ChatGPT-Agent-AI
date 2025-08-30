# File: backend/core/patch_engine/run_manifest.py
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict
import json
import hashlib
import time
import os


def new_run_id(patch_bytes: bytes) -> str:
    h = hashlib.sha256(patch_bytes).hexdigest()[:8]
    ts = time.strftime("%Y-%m-%d_%H-%M-%SZ", time.gmtime())
    return f"{ts}__{h}"


@dataclass
class RunManifest:
    root: Path  # runs/<run_id>
    data: Dict[str, Any] = field(default_factory=dict)

    def _fp(self) -> Path:
        return self.root / "manifest.json"

    def write(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self._fp(), "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def update(self, **kwargs: Any) -> None:
        self.data.update(kwargs)
        self.write()

    def add_section(self, name: str, payload: Dict[str, Any]) -> None:
        self.data.setdefault(name, {})
        self.data[name].update(payload)
        self.write()

    @staticmethod
    def read(path: Path) -> "RunManifest":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return RunManifest(path.parent, data)
