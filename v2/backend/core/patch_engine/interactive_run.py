# File: backend/core/patch_engine/interactive_run.py
# Source baseline: https://raw.githubusercontent.com/gdogg371/ChatGPT-Agent-AI/refs/heads/main/output/patch_code_bundles/backend/core/patch_engine/interactive_run.py
# Changes:
# - Ensure the Scope root is absolute: Scope(root=Path(cfg.mirror_current).resolve(), ...)
from __future__ import annotations

from pathlib import Path
import shutil
import json
import hashlib

from .config import PatchEngineConfig
from .scope import Scope
from .workspace import WorkspaceManager, sha256_dir
from .evaluator import Evaluator, TestPhase
from .applier import PatchApplier
from .run_manifest import RunManifest, new_run_id


def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def run_one(
    patch_path: Path,
    cfg: PatchEngineConfig,
) -> RunManifest:
    """
    Process a single unified-diff patch file end-to-end.

    - Promotion to live mirror is **disabled by default** (cfg.promotion_enabled = False).
    - No Git is used anywhere; patch application is pure Python.
    - Writes a full manifest + artifacts under output/runs/<run_id>/...
    - Returns: RunManifest (already written to disk)
    """
    cfg.ensure_dirs()

    patch_bytes = patch_path.read_bytes()
    run_id = new_run_id(patch_bytes)

    ws = WorkspaceManager(
        mirror_current=cfg.mirror_current,
        snapshots_root=cfg.snapshots_root,
        archives_root=cfg.archives_root,
        keep_last_snapshots=cfg.keep_last_snapshots,
    )

    # Ensure mirror exists (seed if necessary)
    ws.ensure_mirror_seeded(cfg.source_seed_dir)

    # Make run dirs
    rdirs = ws.make_run_dirs(cfg.runs_root, run_id)
    inbox_dir = rdirs["inbox"]
    workspace_dir = rdirs["workspace"]
    apply_dir = rdirs["apply"]
    logs_dir = rdirs["logs"]
    reports_dir = rdirs["reports"]
    artifacts_dir = rdirs["artifacts"]

    # Copy patch into inbox
    inbox_patch = inbox_dir / patch_path.name
    shutil.copy2(patch_path, inbox_patch)
    patch_sha = _sha256_bytes(patch_bytes)
    (inbox_dir / "patch.sha256").write_text(patch_sha, encoding="utf-8")

    manifest = RunManifest(rdirs["base"])
    manifest.update(
        run_id=run_id,
        received={"patch_file": str(inbox_patch), "patch_sha256": patch_sha, "bytes": len(patch_bytes)},
    )

    # Safety: size & touched-file count checks (if configured)
    touched = Scope.parse_patch_paths(_read_text(inbox_patch))
    if cfg.safety and cfg.safety.max_patch_bytes and len(patch_bytes) > cfg.safety.max_patch_bytes:
        manifest.add_section(
            "safety",
            {"reason": "patch_too_large", "limit": cfg.safety.max_patch_bytes, "actual": len(patch_bytes)},
        )
        manifest.add_section("outcome", {"status": "rejected_size"})
        return manifest

    if cfg.safety and cfg.safety.max_touched_files and len(touched) > cfg.safety.max_touched_files:
        manifest.add_section(
            "safety",
            {"reason": "too_many_touched_files", "limit": cfg.safety.max_touched_files, "actual": len(touched)},
        )
        manifest.add_section("outcome", {"status": "rejected_too_many_files"})
        return manifest

    # Scope validation (FIX: ensure root is absolute)
    scope = Scope(root=Path(cfg.mirror_current).resolve(), excludes=cfg.excludes)
    ok, errors = scope.validate_touched_files(touched)
    manifest.add_section(
        "scope",
        {
            "root": str(Path(cfg.mirror_current).resolve()),
            "excludes": cfg.excludes,
            "touched_files": sorted([p.as_posix() for p in touched]),
            "ok": ok,
            "errors": errors,
        },
    )
    if not ok:
        manifest.add_section("outcome", {"status": "rejected_out_of_scope"})
        return manifest

    # Working copy
    ws.clone_to_workspace(workspace_dir)
    manifest.add_section(
        "workspace",
        {"path": str(workspace_dir), "base_mirror_digest": sha256_dir(cfg.mirror_current)},
    )

    # Initial tests
    evaluator = Evaluator(workspace=workspace_dir, logs_dir=logs_dir / "initial", reports_dir=reports_dir)
    initial_res = evaluator.run(TestPhase.INITIAL, cfg.initial_tests)
    manifest.add_section(
        "initial_tests",
        {
            "passed": initial_res.passed,
            "duration_ms": initial_res.duration_ms,
            "reports": initial_res.reports,
            "logs_path": str(initial_res.logs_path),
        },
    )
    if not initial_res.passed:
        manifest.add_section("outcome", {"status": "initial_tests_failed"})
        return manifest

    # Snapshot (pre-change) & optional archive
    snap_id, snap_dir = ws.snapshot_mirror()
    manifest.add_section(
        "snapshot", {"snapshot_id": snap_id, "path": str(snap_dir), "digest": sha256_dir(snap_dir)}
    )
    if cfg.archive_enabled:
        archive_path = ws.archive_snapshot(snap_dir, f"prechange_{snap_id}")
        manifest.add_section("archive", {"path": str(archive_path)})

    # Apply patch (to workspace) — pure Python applier
    applier = PatchApplier(workspace=workspace_dir, apply_dir=apply_dir)
    apply_res = applier.apply_unified_diff(inbox_patch)
    manifest.add_section(
        "apply",
        {
            "dry_run_ok": apply_res.dry_run_ok,
            "applied": apply_res.applied,
            "rejected_hunks": apply_res.rejected_hunks,
            "stdout_path": str(apply_res.stdout_path),
            "rejects_manifest_path": str(apply_res.rejects_manifest_path) if apply_res.rejects_manifest_path else None,
        },
    )
    if not apply_res.dry_run_ok or not apply_res.applied:
        manifest.add_section("outcome", {"status": "apply_failed"})
        return manifest

    # Extensive tests
    evaluator2 = Evaluator(workspace=workspace_dir, logs_dir=logs_dir / "extensive", reports_dir=reports_dir)
    ext_res = evaluator2.run(TestPhase.EXTENSIVE, cfg.extensive_tests)
    manifest.add_section(
        "extensive_tests",
        {
            "passed": ext_res.passed,
            "duration_ms": ext_res.duration_ms,
            "reports": ext_res.reports,
            "logs_path": str(ext_res.logs_path),
        },
    )
    if not ext_res.passed:
        manifest.add_section("outcome", {"status": "extensive_tests_failed"})
        return manifest

    # Promotion is explicitly disabled unless cfg.promotion_enabled is True
    if getattr(cfg, "promotion_enabled", False):
        from .workspace import sha256_dir as _d  # local alias
        ws.promote(workspace_dir)
        manifest.add_section(
            "promotion",
            {
                "status": "promoted",
                "new_mirror_digest": _d(cfg.mirror_current),
                "promotion_enabled": True,
            },
        )
        manifest.add_section("outcome", {"status": "promoted"})
    else:
        manifest.add_section(
            "promotion",
            {
                "status": "skipped",
                "reason": "promotion_disabled",
                "would_promote_from": str(workspace_dir),
                "current_mirror_digest": sha256_dir(cfg.mirror_current),
                "promotion_enabled": False,
            },
        )
        manifest.add_section("outcome", {"status": "would_promote_but_disabled"})

    # Artifacts: changed files (best-effort from patch headers)
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        changed_list = artifacts_dir / "changed_files.txt"
        changed_list.write_text("\n".join(sorted([p.as_posix() for p in touched])) + "\n", encoding="utf-8")
    except Exception:
        pass

    return manifest


if __name__ == "__main__":
    """
    Example interactive usage in PyCharm:
      1) Set 'PATCH_FILE' to a unified diff under output/patches_received/.
      2) Set 'SOURCE_SEED_DIR' to your inscope source root (first run only).
      3) Run this file. With promotion disabled (default), no live mirror changes will be made.
         Flip cfg.promotion_enabled=True to enable.
    """
    # --- EDIT THESE PATHS FOR YOUR ENV ---
    PATCH_FILE = Path("output/patches_received/example.patch")   # <-- point to your patch
    SOURCE_SEED_DIR = Path("v2/backend")                         # <-- seed mirror from this once

    cfg = PatchEngineConfig(
        mirror_current=Path("output/mirrors/current"),
        source_seed_dir=SOURCE_SEED_DIR,
        # Simple built-in tests (can leave empty to skip)
        initial_tests=["python - <<your quick tests here>>"],
        extensive_tests=["python - <<your slower tests here>>"],
        archive_enabled=False,
        promotion_enabled=False,
    )

    # Example run
    run_one(PATCH_FILE, cfg)




