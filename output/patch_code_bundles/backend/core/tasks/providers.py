# File: v2/backend/core/tasks/providers.py
from __future__ import annotations

"""
Generic task utilities exposed as spine capabilities.

Capabilities
------------
- tasks.verify.v1
    Run simple shell commands for verification in a given working directory.

Payload
-------
{
  "cwd": "path/to/workspace",
  "commands": [
     "python - << \"print('hello')\"",
     "pytest -q"
  ],
  "halt_on_failure": true
}

Returns Artifact(kind="Result", meta.result={"reports":[...], "ok": bool})
"""

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from v2.backend.core.spine.contracts import Artifact


def _ok(result: Any) -> List[Artifact]:
    return [Artifact(kind="Result", uri="spine://tasks/ok", sha256="", meta={"result": result})]


def _err(code: str, message: str, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [
        Artifact(
            kind="Problem",
            uri="spine://tasks/error",
            sha256="",
            meta={"problem": {"code": code, "message": message, "retryable": False, "details": dict(details or {})}},
        )
    ]


def verify_v1(payload: Dict[str, Any]) -> List[Artifact]:
    cwd = Path(str(payload.get("cwd") or ".")).resolve()
    commands = [str(c) for c in (payload.get("commands") or [])]
    halt = bool(payload.get("halt_on_failure", True))

    if not commands:
        return _err("NoCommands", "payload.commands is empty")

    reports: List[Dict[str, Any]] = []
    all_ok = True
    for cmd in commands:
        try:
            cp = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True)
            ok = cp.returncode == 0
            reports.append(
                {
                    "cmd": cmd,
                    "returncode": cp.returncode,
                    "ok": ok,
                    "stdout": (cp.stdout or "")[-8000:],
                    "stderr": (cp.stderr or "")[-8000:],
                }
            )
            all_ok = all_ok and ok
            if halt and not ok:
                break
        except Exception as e:
            reports.append({"cmd": cmd, "ok": False, "error": f"{type(e).__name__}: {e}"})
            all_ok = False
            if halt:
                break

    return _ok({"ok": all_ok, "reports": reports})
