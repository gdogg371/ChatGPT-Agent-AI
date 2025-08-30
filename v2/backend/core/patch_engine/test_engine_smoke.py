# File: tests/patch_engine/test_engine_smoke.py
"""
Static tests for the patch engine (no Git required).
These tests simulate a tiny codebase, generate a unified diff, and
exercise scope, apply, and promotion.

Run:
    pytest -q tests/patch_engine/test_engine_smoke.py
"""

from pathlib import Path
import tempfile
import textwrap

from v2.backend.core.patch_engine.config import PatchEngineConfig
from v2.backend.core.patch_engine.scope import Scope
from v2.backend.core.patch_engine.workspace import WorkspaceManager, sha256_dir
from v2.backend.core.patch_engine.applier import PatchApplier
from v2.backend.core.patch_engine.run_manifest import RunManifest, new_run_id


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _make_repo(root: Path) -> None:
    _write(root / "pkg/__init__.py", "__all__ = ['mathx']\n")
    _write(
        root / "pkg/mathx.py",
        textwrap.dedent(
            """
            def add(a, b):
                return a + b
            """
        ).lstrip(),
    )


def _make_patch(old: Path, new: Path) -> Path:
    """
    Create a unified diff patch for 'pkg/mathx.py' changing add() to add with docstring.
    Uses difflib (pure Python).
    """
    import difflib

    old_txt = (old / "pkg/mathx.py").read_text(encoding="utf-8").splitlines(keepends=True)
    new_txt = (new / "pkg/mathx.py").read_text(encoding="utf-8").splitlines(keepends=True)

    diff = difflib.unified_diff(
        old_txt,
        new_txt,
        fromfile="a/pkg/mathx.py",
        tofile="b/pkg/mathx.py",
        lineterm="",
        n=3,
    )
    patch_text = "\n".join(diff) + "\n"
    patch_file = old.parent / "example.patch"
    patch_file.write_text(patch_text, encoding="utf-8")
    return patch_file


def test_end_to_end_apply_and_promote(tmp_path: Path):
    # Prepare a seed source tree
    seed = tmp_path / "seed"
    _make_repo(seed)

    # Prepare a modified copy to diff against
    modified = tmp_path / "modified"
    _make_repo(modified)
    _write(
        modified / "pkg/mathx.py",
        textwrap.dedent(
            """
            def add(a, b):
                \"\"\"Add two numbers (patched).\"\"\"\n                return a + b
            """
        ).lstrip(),
    )

    # Generate patch file
    patch_file = _make_patch(seed, modified)

    # Prepare config (file-only)
    cfg = PatchEngineConfig(
        mirror_current=tmp_path / "out/mirrors/current",
        source_seed_dir=seed,
        initial_tests=[],   # keep empty for test speed
        extensive_tests=[], # keep empty for test speed
        archive_enabled=True,
        keep_last_snapshots=2,
    )

    # Ensure dirs and seed mirror
    cfg.ensure_dirs()
    ws = WorkspaceManager(cfg.mirror_current, cfg.snapshots_root, cfg.archives_root, cfg.keep_last_snapshots)
    ws.ensure_mirror_seeded(cfg.source_seed_dir)

    # Scope & touched paths
    scope = Scope(root=cfg.mirror_current, excludes=cfg.excludes)
    touched = Scope.parse_patch_paths(patch_file.read_text(encoding="utf-8"))
    ok, errors = scope.validate_touched_files(touched)
    assert ok, f"scope errors: {errors}"

    # Create run and working copy
    run_id = new_run_id(patch_file.read_bytes())
    rdirs = ws.make_run_dirs(cfg.runs_root, run_id)
    ws.clone_to_workspace(rdirs["workspace"])

    # Snapshot mirror
    snap_id, snap_dir = ws.snapshot_mirror()
    assert snap_dir.exists()

    # Apply patch to workspace (pure Python)
    ap = PatchApplier(workspace=rdirs["workspace"], apply_dir=rdirs["apply"])
    res = ap.apply_unified_diff(patch_file)
    assert res.dry_run_ok, "dry run failed"
    assert res.applied, "apply failed"
    assert res.rejected_hunks == 0, "unexpected rejects"

    # Promote to mirror (explicit in test)
    before_digest = sha256_dir(cfg.mirror_current)
    ws.promote(rdirs["workspace"])
    after_digest = sha256_dir(cfg.mirror_current)
    assert before_digest != after_digest, "mirror digest should change after promotion"

    # Verify new content in mirror
    final = (cfg.mirror_current / "pkg/mathx.py").read_text(encoding="utf-8")
    assert "patched" in final

    # Write a simple manifest and read back
    manifest = RunManifest(rdirs["base"])
    manifest.update(run_id=run_id, outcome={"status": "promoted"})
    loaded = RunManifest.read(rdirs["base"] / "manifest.json")
    assert loaded.data["outcome"]["status"] == "promoted"

