# File: backend/core/patch_engine/evaluator.py
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Dict, Any
import subprocess
import time
import json


class TestPhase(str, Enum):
    INITIAL = "initial"
    EXTENSIVE = "extensive"


@dataclass
class TestResult:
    phase: TestPhase
    passed: bool
    duration_ms: int
    reports: Dict[str, Any]
    logs_path: Path


class Evaluator:
    """
    Executes provided shell commands as tests.
    - commands: list[str] executed with shell=True, cwd=workspace
    - writing stdout/stderr logs per command to logs_dir
    - returns TestResult with structured summary

    NOTE: Commands may be empty → treated as pass.
    """

    def __init__(self, workspace: Path, logs_dir: Path, reports_dir: Path):
        self.workspace = workspace
        self.logs_dir = logs_dir
        self.reports_dir = reports_dir

    def _run_command(self, cmd: str, idx: int) -> tuple[bool, float, Path]:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_fp = self.logs_dir / f"cmd_{idx:02d}.log"
        t0 = time.perf_counter()
        proc = subprocess.run(cmd, cwd=self.workspace, shell=True, capture_output=True, text=True)
        dt = time.perf_counter() - t0
        with open(log_fp, "w", encoding="utf-8", newline="") as f:
            f.write(f"$ {cmd}\n\n")
            f.write(proc.stdout or "")
            if proc.stderr:
                f.write("\n[stderr]\n" + proc.stderr)
            f.write(f"\n\n[exit] {proc.returncode}\n")
        return proc.returncode == 0, dt, log_fp

    def run(self, phase: TestPhase, commands: List[str]) -> TestResult:
        if not commands:
            # no tests requested => pass
            return TestResult(
                phase=phase,
                passed=True,
                duration_ms=0,
                reports={},
                logs_path=self.logs_dir / f"{phase}_empty.log",
            )

        total_ok = True
        total_ms = 0.0
        logs_index: Dict[str, str] = {}
        for i, cmd in enumerate(commands, start=1):
            ok, dt, log_fp = self._run_command(cmd, i)
            logs_index[f"cmd_{i:02d}"] = log_fp.name
            total_ok = total_ok and ok
            total_ms += dt

        # Write a small reports index JSON
        reports = {"logs": logs_index}
        reports_fp = self.reports_dir / f"{phase}_reports.json"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        with open(reports_fp, "w", encoding="utf-8") as f:
            json.dump(reports, f, indent=2)
        return TestResult(
            phase=phase,
            passed=total_ok,
            duration_ms=int(total_ms * 1000),
            reports=reports,
            logs_path=reports_fp,
        )
