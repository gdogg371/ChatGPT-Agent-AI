# v2/backend/core/utils/code_bundles/code_bundles/execute/github_api.py

from __future__ import annotations
import json
import sys
from pathlib import Path
from urllib import error, parse, request

# Ensure the embedded packager is importable first
ROOT = Path(__file__).parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

__all__ = ["_gh_headers", "_gh_json", "_gh_delete_file", "_gh_list_dir", "_gh_walk_files"]


def _gh_headers(token: str) -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "code-bundles-packager",
    }


def _gh_json(req: request.Request) -> dict:
    with request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))


def _gh_delete_file(owner: str, repo: str, path: str, sha: str, token: str, message: str) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{parse.quote(path)}"
    payload = json.dumps({"message": message, "sha": sha, "branch": "main"}).encode("utf-8")
    req = request.Request(url, data=payload, method="DELETE", headers=_gh_headers(token))
    _gh_json(req)


def _gh_list_dir(owner: str, repo: str, dir_path: str, token: str) -> list:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{parse.quote(dir_path)}"
    req = request.Request(url, headers=_gh_headers(token))
    return _gh_json(req)


def _gh_walk_files(owner: str, repo: str, dir_path: str, token: str) -> list:
    items = _gh_list_dir(owner, repo, dir_path, token) or []
    out = []
    for it in items:
        if it.get("type") == "file":
            out.append(it["path"])
        elif it.get("type") == "dir":
            out.extend(_gh_walk_files(owner, repo, it["path"], token))
    return out
