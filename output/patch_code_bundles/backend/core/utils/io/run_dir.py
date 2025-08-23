from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

@dataclass
class RunDirs:
    out_base: Path

    def make_run_id(self, suffix: Optional[str] = None) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{ts}_{suffix}" if suffix else ts

    def ensure(self, run_id: str):
        root = self.out_base / run_id
        sub = [
            "archives", "patches", "prod_applied", "raw_prompts",
            "raw_responses", "rollbacks", "sandbox_applied", "verify_reports"
        ]
        root.mkdir(parents=True, exist_ok=True)
        for name in sub: (root / name).mkdir(exist_ok=True)
        # legacy alias
        (root / "verify reports").mkdir(exist_ok=True)
        return type("RunDir", (), {"root": root, **{n: root / n for n in sub}})()
