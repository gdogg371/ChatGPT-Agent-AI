# v2/backend/core/utils/code_bundles/code_bundles/execute/publish.py

from __future__ import annotations
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from typing import List
from urllib import parse, request

# Ensure the embedded packager is importable first
ROOT = Path(__file__).parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Cross-module imports
from .github_api import (_gh_headers, _gh_json, _gh_delete_file, _gh_list_dir, _gh_walk_files)

__all__ = ["github_clean_remote_repo", "_publish_to_github", "_prune_remote_code_delta", "_prune_remote_artifacts_delta"]


def github_clean_remote_repo(owner: str, repo: str, base_dir: str, token: str) -> None:
    listing = _gh_list_dir(owner, repo, base_dir, token) or []
    for it in listing:
        if it.get("type") == "file":
            _gh_delete_file(owner, repo, it["path"], it["sha"], token, f"cleanup: remove {it['path']}")


def _publish_to_github(cfg: NS, local_dir: Path, remote_base: str) -> None:
    owner = cfg.github_owner
    repo = cfg.github_repo
    token = cfg.github_token
    base = remote_base.strip("/")

    for p in sorted(Path(local_dir).rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(local_dir).as_posix()
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{parse.quote(f'{base}/{rel}')}"
        content_b64 = Path(p).read_bytes()
        import base64
        payload = json.dumps({
            "message": f"publish {rel}",
            "content": base64.b64encode(content_b64).decode("ascii"),
            "branch": cfg.github_branch,
        }).encode("utf-8")
        req = request.Request(url, data=payload, method="PUT", headers=_gh_headers(token))
        _gh_json(req)


def _prune_remote_code_delta(cfg: NS, remote_listing: List[str], local_listing: List[str]) -> List[str]:
    base = (cfg.github_base or "").strip("/")

    remote_rel = [p for p in remote_listing if p.startswith(f"{base}/v2/") or p.startswith(f"{base}/config/") or p.startswith(f"{base}/scripts/") or p.startswith(f"{base}/recovery/") or p.startswith(f"{base}/cold_start/")]
    local_rel = [f"{base}/{str(Path(p).as_posix())}" for p in local_listing]

    deletions = sorted(set(remote_rel) - set(local_rel))
    return deletions


def _prune_remote_artifacts_delta(cfg: NS, remote_listing: List[str], local_listing: List[str]) -> List[str]:
    base = (cfg.github_base or "").strip("/")
    remote_rel = [p for p in remote_listing if p.startswith(f"{base}/design_manifest/")]
    local_rel = [f"{base}/design_manifest/{str(Path(p).relative_to(Path(cfg.project_root) / 'design_manifest').as_posix())}" for p in local_listing]
    deletions = sorted(set(remote_rel) - set(local_rel))
    return deletions
