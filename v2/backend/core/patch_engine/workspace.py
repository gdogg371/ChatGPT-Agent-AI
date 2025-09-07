# File: backend/core/patch_engine/workspace.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import hashlib
import time


def now_ts() -> str:
    return time.strftime("%Y-%m-%d_%H-%M-%SZ", time.gmtime())


def sha256_dir(root: Path) -> str:
    sha = hashlib.sha256()
    root = root.resolve()
    for dirpath, _, filenames in os.walk(root):
        for name in sorted(filenames):
            fp = Path(dirpath) / name
            rel = fp.relative_to(root).as_posix()
            sha.update(rel.encode("utf-8"))
            try:
                with open(fp, "rb") as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        sha.update(chunk)
            except Exception:
                # Non-fatal; skip unreadable files
                continue
    return sha.hexdigest()


@dataclass
class WorkspaceManager:
    mirror_current: Path
    snapshots_root: Path
    archives_root: Path
    keep_last_snapshots: int = 5

    def ensure_mirror_seeded(self, seed_dir: Path | None) -> None:
        """
        If mirror is empty and a seed_dir is provided, copy seed_dir → mirror.
        """
        self.mirror_current.parent.mkdir(parents=True, exist_ok=True)
        if self.mirror_current.exists() and any(self.mirror_current.rglob("*")):
            return
        if not seed_dir:
            raise RuntimeError(
                f"Mirror '{self.mirror_current}' is empty; provide source_seed_dir to seed."
            )
        shutil.copytree(seed_dir, self.mirror_current, dirs_exist_ok=True)

    def make_run_dirs(self, runs_root: Path, run_id: str) -> dict[str, Path]:
        base = runs_root / run_id
        dirs = {
            "base": base,
            "inbox": base / "inbox",
            "workspace": base / "workspace",
            "apply": base / "apply",
            "logs": base / "logs",
            "reports": base / "reports",
            "artifacts": base / "artifacts",
            "manifests": base / "manifests",
        }
        for p in dirs.values():
            p.mkdir(parents=True, exist_ok=True)
        return dirs

    def clone_to_workspace(self, workspace_dir: Path) -> None:
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir)
        shutil.copytree(self.mirror_current, workspace_dir)

    def snapshot_mirror(self) -> tuple[str, Path]:
        snap_id = now_ts()
        dest = self.snapshots_root / snap_id
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(self.mirror_current, dest)
        self._gc_old_snapshots()
        return snap_id, dest

    def promote(self, from_workspace: Path) -> None:
        """
        Replace mirror_current with the content of 'from_workspace' atomically.
        """
        parent = self.mirror_current.parent
        staging = parent / (self.mirror_current.name + "__staging")
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(from_workspace, staging)

        tmp_old = parent / (self.mirror_current.name + "__old")
        if tmp_old.exists():
            shutil.rmtree(tmp_old)
        if self.mirror_current.exists():
            self.mirror_current.rename(tmp_old)
        staging.rename(self.mirror_current)
        if tmp_old.exists():
            shutil.rmtree(tmp_old)

    def archive_snapshot(self, snapshot_dir: Path, name: str) -> Path:
        """
        Create a zip archive of snapshot_dir in archives_root with base name 'name'.
        """
        self.archives_root.mkdir(parents=True, exist_ok=True)
        archive_path = self.archives_root / f"{name}.zip"
        # shutil.make_archive requires path without the .zip suffix
        base_no_ext = archive_path.with_suffix("")
        if base_no_ext.exists():
            base_no_ext.unlink()
        shutil.make_archive(str(base_no_ext), "zip", snapshot_dir)
        return archive_path

    def mirror_digest(self) -> str:
        return sha256_dir(self.mirror_current)

    def _gc_old_snapshots(self) -> None:
        snaps = sorted([p for p in self.snapshots_root.iterdir() if p.is_dir()], key=lambda p: p.name)
        if len(snaps) <= self.keep_last_snapshots:
            return
        for p in snaps[:-self.keep_last_snapshots]:
            shutil.rmtree(p, ignore_errors=True)
