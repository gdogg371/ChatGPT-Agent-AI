from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import List, Optional
from urllib import request, parse, error

from types import SimpleNamespace as NS

# Reuse the low-level GitHub helpers from the sibling module
from .github_api import (
    _gh_headers,
    _gh_json,
    _gh_delete_file,
    _gh_list_dir,
    _gh_walk_files,
)

__all__ = [
    "github_clean_remote_repo",
    "_publish_to_github",
    "_prune_remote_code_delta",
    "_prune_remote_artifacts_delta",
]


# -------------------------
# Internal HTTP convenience
# -------------------------

def _gh_get_file_meta(owner: str, repo: str, path: str, token: str, ref: Optional[str] = None) -> Optional[dict]:
    """
    Return GitHub 'contents' metadata for a file path (including 'sha') or None if not found.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{parse.quote(path)}"
    if ref:
        url += f"?ref={parse.quote(ref)}"
    req = request.Request(url, headers=_gh_headers(token))
    try:
        meta = _gh_json(req)
        if isinstance(meta, dict) and meta.get("type") == "file":
            return meta
        return None
    except error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _compose_remote_path(base: str, rel: str) -> str:
    base = (base or "").strip("/")
    rel = rel.lstrip("/")
    return f"{base}/{rel}" if base else rel


def _iter_local_files(local_dir: Path):
    local_dir = Path(local_dir)
    for p in sorted(local_dir.rglob("*")):
        if p.is_file():
            yield p


# -------------------------
# Public API
# -------------------------

def github_clean_remote_repo(owner: str, repo: str, base_dir: str, token: str) -> None:
    """
    Delete all files directly under base_dir/ in the GitHub repo.
    Does not recurse into subdirectories (mirrors original behavior of listing only the dir passed).
    """
    if not owner or not repo:
        raise ValueError("owner and repo are required.")
    dir_path = (base_dir or "").strip("/")
    listing = _gh_list_dir(owner, repo, dir_path, token) or []
    for it in listing:
        if it.get("type") == "file":
            _gh_delete_file(owner, repo, it["path"], it["sha"], token, f"cleanup: remove {it['path']}")


def _publish_to_github(cfg: NS, local_dir: Path, remote_base: str) -> None:
    """
    Publish all files from local_dir into the GitHub repo under remote_base.
    Creates new files or updates existing ones (includes sha when needed).
    """
    owner = (getattr(cfg, "github_owner", None) or "").strip()
    repo = (getattr(cfg, "github_repo", None) or "").strip()
    token = (getattr(cfg, "github_token", None) or "").strip()
    branch = (getattr(cfg, "github_branch", None) or "main").strip()

    if not owner or not repo or not token:
        raise ValueError("cfg.github_owner, cfg.github_repo, and cfg.github_token must be set.")
    local_dir = Path(local_dir)
    if not local_dir.exists():
        raise FileNotFoundError(f"Local directory not found: {local_dir}")

    base = (remote_base or "").strip("/")

    for p in _iter_local_files(local_dir):
        rel = p.relative_to(local_dir).as_posix()
        remote_path = _compose_remote_path(base, rel)
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{parse.quote(remote_path)}"

        content_b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        payload = {
            "message": f"publish {rel}",
            "content": content_b64,
            "branch": branch,
        }

        # If file exists, include its current sha to update instead of create
        meta = _gh_get_file_meta(owner, repo, remote_path, token, ref=branch)
        if meta and "sha" in meta:
            payload["sha"] = meta["sha"]

        req = request.Request(url, data=json.dumps(payload).encode("utf-8"), method="PUT", headers=_gh_headers(token))
        _gh_json(req)


def _prune_remote_code_delta(cfg: NS, remote_listing: List[str], local_listing: List[str]) -> List[str]:
    """
    Compute remote code files to delete: (remote code subset) - (local code set).

    Assumes both remote_listing and local_listing are repo paths using forward slashes.
    Filters to typical code roots under cfg.github_base: v2/, config/, scripts/, recovery/, cold_start/.
    """
    base = (getattr(cfg, "github_base", "") or "").strip("/")
    prefix = f"{base}/" if base else ""

    code_roots = ("v2/", "config/", "scripts/", "recovery/", "cold_start/")
    def _is_code_path(p: str) -> bool:
        return any(p.startswith(prefix + root) for root in code_roots)

    remote_set = {p for p in remote_listing if _is_code_path(p)}
    local_set = { (prefix + lp.lstrip("/")) for lp in local_listing if _is_code_path(prefix + lp.lstrip("/")) }

    deletions = sorted(remote_set - local_set)
    return deletions


def _prune_remote_artifacts_delta(cfg: NS, remote_listing: List[str], local_listing: List[str]) -> List[str]:
    """
    Compute remote artifact files to delete: (remote artifacts subset) - (local artifact set).

    Focuses under {base}/design_manifest/**.
    Assumes local_listing is a list of repo paths (or paths relative to design_manifest/).
    """
    base = (getattr(cfg, "github_base", "") or "").strip("/")
    prefix = f"{base}/" if base else ""
    artifact_root = prefix + "design_manifest/"

    def _to_repo_path(p: str) -> str:
        # If caller passed a path relative to design_manifest/, normalize it under the base.
        p = p.replace("\\", "/")
        if p.startswith(artifact_root):
            return p
        if p.startswith("design_manifest/"):
            return prefix + p
        return artifact_root + p.lstrip("/")

    remote_set = {p for p in remote_listing if p.startswith(artifact_root)}
    local_set = {_to_repo_path(p) for p in local_listing}

    deletions = sorted(remote_set - local_set)
    return deletions
