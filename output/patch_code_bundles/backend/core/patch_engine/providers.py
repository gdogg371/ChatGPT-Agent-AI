# File: v2/backend/core/patch_engine/providers.py
from __future__ import annotations

"""
Spine providers for patch engine operations.

Capabilities
------------
- patch.run.v1
    Thin orchestrator that forwards to llm.engine.run.v1 with the given payload.
    This is kept to preserve historical naming used by some CLIs.

- patch.apply_files.v1
    Apply a list of *.patch files to a mirror workspace using the pure-Python
    PatchApplier. Optionally promote to mirror and/or archive snapshots.

Payloads
--------
patch.run.v1:
  { ... }  # engine payload; forwarded verbatim to llm.engine.run.v1

patch.apply_files.v1:
  {
    "patches": ["/abs/path/to/change.patch", ...],
    "mirror_current": "output/mirrors/current",
    "source_seed_dir": "v2/backend",             # used if mirror is empty
    "initial_tests": ["python - << \"print(1)\""],
    "extensive_tests": [],
    "excludes": ["**/.git/**","**/__pycache__/**","**/output/**"],
    "promotion_enabled": false,
  }
"""

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from v2.backend.core.spine import Spine
from v2.backend.core.spine.contracts import Artifact
from v2.backend.core.spine.contracts import to_dict as artifact_to_dict
from v2.backend.core.configuration.config import SPINE_CAPS_PATH
from v2.backend.core.patch_engine.applier import PatchApplier
from v2.backend.core.patch_engine.workspace import WorkspaceManager, now_ts


def _ok(result: Any, *, kind: str = "Result") -> List[Artifact]:
    return [
        Artifact(
            kind=kind,
            uri="spine://patch/ok",
            sha256="",
            meta={"result": result},
        )
    ]


def _err(code: str, message: str, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [
        Artifact(
            kind="Problem",
            uri="spine://patch/error",
            sha256="",
            meta={
                "problem": {
                    "code": code,
                    "message": message,
                    "retryable": False,
                    "details": dict(details or {}),
                }
            },
        )
    ]


# --------------------------- patch.run.v1 --------------------------------------

def run_v1(payload: Dict[str, Any]) -> List[Artifact]:
    """Forward to llm.engine.run.v1 for backwards compatibility."""
    spine = Spine(caps_path=SPINE_CAPS_PATH)
    return spine.dispatch_capability(
        capability="llm.engine.run.v1",
        payload=payload,
        intent="pipeline",
        subject=str(payload.get("project_root") or "-"),
        context={"provider": "patch_engine.run_v1"},
    )


# ----------------------- patch.apply_files.v1 ----------------------------------

def _run_tests(cmds: List[str], cwd: Path) -> Tuple[bool, List[Dict[str, Any]]]:
    results: List[Dict[str, Any]] = []
    ok_all = True
    for cmd in cmds or []:
        try:
            cp = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True)
            ok = cp.returncode == 0
            ok_all = ok_all and ok
            results.append(
                {
                    "cmd": cmd,
                    "returncode": cp.returncode,
                    "stdout": (cp.stdout or "")[-8000:],
                    "stderr": (cp.stderr or "")[-8000:],
                    "ok": ok,
                }
            )
        except Exception as e:
            ok_all = False
            results.append({"cmd": cmd, "error": f"{type(e).__name__}: {e}", "ok": False})
    return ok_all, results


def apply_files_v1(payload: Dict[str, Any]) -> List[Artifact]:
    patches = [Path(str(p)).resolve() for p in (payload.get("patches") or [])]
    if not patches:
        return _err("NoPatches", "payload.patches is empty")

    mirror_current = Path(str(payload.get("mirror_current") or "output/mirrors/current")).resolve()
    runs_root = mirror_current.parent / "runs"
    snapshots_root = mirror_current.parent / "snapshots"
    archives_root = mirror_current.parent / "archives"

    wm = WorkspaceManager(
        mirror_current=mirror_current,
        snapshots_root=snapshots_root,
        archives_root=archives_root,
    )

    # Seed mirror if needed
    seed = payload.get("source_seed_dir")
    if seed:
        wm.ensure_mirror_seeded(Path(str(seed)).resolve())

    # Prepare run dirs
    run_id = now_ts()
    dirs = wm.make_run_dirs(runs_root, run_id)
    workspace = dirs["workspace"]
    apply_dir = dirs["apply"]
    logs_dir = dirs["logs"]

    # Clone mirror to workspace
    wm.clone_to_workspace(workspace)

    # Apply patches (all must succeed)
    applier = PatchApplier(workspace=workspace, apply_dir=apply_dir)
    apply_reports: List[Dict[str, Any]] = []
    rejected_total = 0
    for pf in patches:
        res = applier.apply_unified_diff(pf)
        rejected_total += int(res.rejected_hunks)
        apply_reports.append(
            {
                "patch": str(pf),
                "dry_run_ok": bool(res.dry_run_ok),
                "applied": bool(res.applied),
                "rejected_hunks": int(res.rejected_hunks),
                "stdout_path": str(res.stdout_path),
                "rejects_manifest_path": str(res.rejects_manifest_path) if res.rejects_manifest_path else None,
            }
        )

    # If any rejects, stop here
    if rejected_total > 0:
        return _ok(
            {
                "run_id": run_id,
                "workspace": str(workspace),
                "apply_reports": apply_reports,
                "outcome": {"status": "initial_tests_failed", "reason": f"{rejected_total} hunks rejected"},
            }
        )

    # Optional: run initial + extensive tests
    initial_cmds = list(payload.get("initial_tests") or [])
    extensive_cmds = list(payload.get("extensive_tests") or [])

    init_ok, init_reports = _run_tests(initial_cmds, workspace)
    if not init_ok:
        return _ok(
            {
                "run_id": run_id,
                "workspace": str(workspace),
                "apply_reports": apply_reports,
                "initial_tests": init_reports,
                "outcome": {"status": "initial_tests_failed"},
            }
        )

    ext_ok, ext_reports = _run_tests(extensive_cmds, workspace) if extensive_cmds else (True, [])

    # Promotion & archiving
    promoted = False
    archive_path = None
    if init_ok and ext_ok and bool(payload.get("promotion_enabled", False)):
        snap_id, snap_dir = wm.snapshot_mirror()
        wm.promote(workspace)
        archive_path = str(wm.archive_snapshot(snap_dir, f"{run_id}__{snap_id}"))
        promoted = True

    status = (
        "promoted"
        if promoted
        else ("would_promote_but_disabled" if init_ok and ext_ok else "extensive_tests_failed")
    )

    return _ok(
        {
            "run_id": run_id,
            "workspace": str(workspace),
            "apply_reports": apply_reports,
            "initial_tests": init_reports,
            "extensive_tests": ext_reports,
            "outcome": {"status": status, "archive": archive_path},
        }
    )
