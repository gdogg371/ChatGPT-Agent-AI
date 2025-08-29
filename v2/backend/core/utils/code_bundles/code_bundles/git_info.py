# File: v2/backend/core/utils/code_bundles/code_bundles/git_info.py
"""
Git repository metadata (stdlib-only).

Emits JSONL-ready records that capture lightweight Git state without any
third-party libraries. If Git is unavailable or the path is not a Git repo,
the scanner returns a minimal record with "available": false.

Records
-------
git.repo
  Summary of the repository: HEAD, branch, remotes, ahead/behind, dirty flag,
  commit counts and dates, tracked/untracked counts.

git.ignore
  One record per .gitignore discovered (relative path + non-comment patterns).

git.submodule
  One record per submodule entry in .gitmodules (path, url, branch).

git.info.summary
  Aggregated counts across the above (files, ignores, submodules) so consumers
  can get a quick view without scanning the whole stream.

Notes
-----
* Uses only the standard library + the 'git' executable via subprocess.
* Paths are repo-relative POSIX. If your pipeline distinguishes local vs GitHub
  path modes, the caller should map 'path' before appending to the manifest.
"""

from __future__ import annotations

import configparser
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

RepoItem = Tuple[Path, str]  # (local_path, repo_relative_posix)


# ──────────────────────────────────────────────────────────────────────────────
# Subprocess helpers
# ──────────────────────────────────────────────────────────────────────────────

def _run_git(args: List[str], cwd: Path, timeout: int = 15) -> Optional[str]:
    """
    Run a git command and return stdout (stripped). Returns None on failure.
    """
    try:
        completed = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()
    except Exception:
        return None


def _has_git(cwd: Path) -> bool:
    out = _run_git(["--version"], cwd=cwd)
    return bool(out and out.startswith("git version"))


def _is_repo(cwd: Path) -> bool:
    out = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=cwd)
    return bool(out and out.strip().lower() == "true")


# ──────────────────────────────────────────────────────────────────────────────
# Data collectors
# ──────────────────────────────────────────────────────────────────────────────

def _collect_repo_info(repo_root: Path) -> Dict:
    """
    Collect high-level repo facts. Robust to detached HEAD and missing upstreams.
    """
    info: Dict = {
        "kind": "git.repo",
        "path": ".",
        "available": False,
        "head": {},
        "remotes": [],
        "status": {},
        "commits": {},
    }

    if not _has_git(repo_root) or not _is_repo(repo_root):
        return info

    info["available"] = True

    # HEAD commit & branch
    head_commit = _run_git(["rev-parse", "HEAD"], repo_root)
    head_ref = _run_git(["symbolic-ref", "-q", "HEAD"], repo_root)  # may be None on detached
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)  # "HEAD" if detached
    describe = _run_git(["describe", "--tags", "--always", "--dirty"], repo_root)

    head = {
        "commit": head_commit,
        "ref": head_ref,
        "branch": branch,
        "describe": describe,
    }
    info["head"] = head

    # Remotes (fetch & push)
    rem_map: Dict[str, Dict[str, Optional[str]]] = {}
    rem_out = _run_git(["remote", "-v"], repo_root) or ""
    # Lines like: origin  https://... (fetch)
    for line in rem_out.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and parts[-1] in ("(fetch)", "(push)"):
            name = parts[0]
            url = parts[1]
            kind = parts[2].strip("()")
            rem = rem_map.setdefault(name, {"name": name, "fetch_url": None, "push_url": None, "url": None, "default": False})
            if kind == "fetch":
                rem["fetch_url"] = url
            elif kind == "push":
                rem["push_url"] = url
            # Keep a generic 'url' for convenience (prefer fetch)
            rem["url"] = rem["fetch_url"] or rem["push_url"] or url
    # Mark "origin" as default if present
    if "origin" in rem_map:
        rem_map["origin"]["default"] = True
    info["remotes"] = list(rem_map.values())

    # Upstream ahead/behind (optional)
    ahead = behind = None
    upstream = _run_git(["rev-parse", "--abbrev-ref", "@{upstream}"], repo_root)
    if upstream:
        lr = _run_git(["rev-list", "--left-right", "--count", f"{upstream}...HEAD"], repo_root)
        if lr:
            # e.g., "2\t5" => behind=2 ahead=5
            parts = lr.split()
            if len(parts) >= 2:
                try:
                    behind = int(parts[0])
                    ahead = int(parts[1])
                except Exception:
                    pass

    # Dirty?
    porcelain = _run_git(["status", "--porcelain"], repo_root) or ""
    is_dirty = any(line.strip() for line in porcelain.splitlines())

    info["status"] = {"is_dirty": bool(is_dirty), "ahead": ahead, "behind": behind}

    # Commit counts & dates
    count = _run_git(["rev-list", "--count", "HEAD"], repo_root)
    try:
        count_i = int(count) if count and count.isdigit() else None
    except Exception:
        count_i = None

    # First commit date (ISO-8601)
    first_hash = _run_git(["rev-list", "--max-parents=0", "HEAD"], repo_root)
    first_date = _run_git(["show", "-s", "--format=%cI", first_hash], repo_root) if first_hash else None
    last_date = _run_git(["show", "-s", "--format=%cI", "HEAD"], repo_root)

    # Tracked/untracked counts
    tracked = _run_git(["ls-files"], repo_root) or ""
    untracked = _run_git(["ls-files", "--others", "--exclude-standard"], repo_root) or ""
    info["commits"] = {
        "count": count_i,
        "first_date": first_date,
        "last_date": last_date,
        "tracked_files": len([1 for _ in tracked.splitlines() if _ != ""]),
        "untracked_files": len([1 for _ in untracked.splitlines() if _ != ""]),
    }

    return info


def _collect_gitignores(repo_root: Path, discovered: Iterable[RepoItem]) -> List[Dict]:
    """
    One record per .gitignore discovered anywhere in the tree.
    """
    out: List[Dict] = []
    for local, rel in discovered:
        if Path(rel).name != ".gitignore":
            continue
        try:
            text = local.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        patterns: List[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
        out.append({
            "kind": "git.ignore",
            "path": rel,
            "patterns": patterns,
            "patterns_count": len(patterns),
        })
    return out


def _collect_submodules(repo_root: Path) -> List[Dict]:
    """
    Parse .gitmodules (if present) to emit one record per submodule.
    """
    out: List[Dict] = []
    gm = repo_root / ".gitmodules"
    if not gm.exists():
        return out

    cfg = configparser.ConfigParser()
    try:
        # Git style allows keys without quotes; ConfigParser handles section names like: submodule "path"
        cfg.read(gm, encoding="utf-8")
    except Exception:
        return out

    # Sections look like: submodule "path/to/sub"
    for sec in cfg.sections():
        if not sec.lower().startswith("submodule"):
            continue
        name = sec
        path = cfg.get(sec, "path", fallback=None)
        url = cfg.get(sec, "url", fallback=None)
        branch = cfg.get(sec, "branch", fallback=None)
        out.append({
            "kind": "git.submodule",
            "path": path or "",
            "name": name,
            "url": url,
            "branch": branch,
        })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def scan(repo_root: Path, discovered: Iterable[RepoItem]) -> List[Dict]:
    """
    Collect Git metadata for the repository rooted at 'repo_root'.

    Returns:
      - git.repo (always one, even if Git unavailable)
      - git.ignore (0..N)
      - git.submodule (0..N)
      - git.info.summary (always one)
    """
    repo_root = Path(repo_root)
    discovered = list(discovered)

    records: List[Dict] = []

    repo_rec = _collect_repo_info(repo_root)
    records.append(repo_rec)

    ignore_recs = _collect_gitignores(repo_root, discovered)
    records.extend(ignore_recs)

    submods = _collect_submodules(repo_root)
    records.extend(submods)

    summary = {
        "kind": "git.info.summary",
        "available": bool(repo_rec.get("available")),
        "ignores": len(ignore_recs),
        "submodules": len(submods),
        "remotes": len(repo_rec.get("remotes") or []),
        "tracked_files": (repo_rec.get("commits") or {}).get("tracked_files"),
        "untracked_files": (repo_rec.get("commits") or {}).get("untracked_files"),
        "dirty": (repo_rec.get("status") or {}).get("is_dirty"),
    }
    records.append(summary)

    return records


__all__ = ["scan"]
