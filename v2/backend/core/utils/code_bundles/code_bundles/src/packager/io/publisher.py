# File: output/patch_code_bundles/backend/core/utils/code_bundles/code_bundles/src/packager/io/publisher.py
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple
from urllib import request, error, parse


@dataclass(frozen=True)
class GitHubTarget:
    owner: str
    repo: str
    branch: str = "main"
    base_path: str = ""  # path prefix inside the repo


def _headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "code-bundles-packager",
        "Content-Type": "application/json; charset=utf-8",
    }


def _api_url(owner: str, repo: str, path: str) -> str:
    path = path.lstrip("/")
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{parse.quote(path)}"


def _get_sha(target: GitHubTarget, path: str, token: str) -> Optional[str]:
    """Return current file SHA if the path exists on GitHub, else None."""
    url = _api_url(target.owner, target.repo, path) + f"?ref={parse.quote(target.branch)}"
    req = request.Request(url, headers=_headers(token), method="GET")
    try:
        with request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and data.get("type") == "file":
            return data.get("sha")
        return None
    except error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _put_file(target: GitHubTarget, path: str, content_b64: str, token: str, message: str, sha: Optional[str]) -> None:
    body = {"message": message, "branch": target.branch, "content": content_b64}
    if sha:
        body["sha"] = sha
    url = _api_url(target.owner, target.repo, path)
    req = request.Request(url, data=json.dumps(body).encode("utf-8"), headers=_headers(token), method="PUT")
    with request.urlopen(req, timeout=30) as resp:
        resp.read()  # drain


def _join_paths(prefix: str, rel: str) -> str:
    prefix = (prefix or "").strip("/ ")
    rel = rel.lstrip("/ ")
    return f"{prefix}/{rel}" if prefix else rel


class GitHubPublisher:
    """
    Simple GitHub publisher using the Contents API.
    - Creates or updates files under base_path on the specified branch.
    - Uses PUT /contents/{path} with base64-encoded content.
    """

    def __init__(self, target: GitHubTarget, token: str) -> None:
        if not token:
            raise ValueError("GitHubPublisher: token is empty")
        self.target = target
        self.token = token

    def publish_bytes(self, repo_rel_path: str, data: bytes, message: str = "publish") -> None:
        """
        Publish a single in-memory payload to GitHub at repo_rel_path (under base_path).
        """
        path = _join_paths(self.target.base_path, repo_rel_path)
        sha = _get_sha(self.target, path, self.token)
        content_b64 = base64.b64encode(data).decode("ascii")
        _put_file(self.target, path, content_b64, self.token, message, sha)

    def publish_file(self, src_path: Path, repo_rel_path: str, message: str = "publish") -> None:
        """
        Publish a local file to GitHub at repo_rel_path (under base_path).
        """
        data = src_path.read_bytes()
        self.publish_bytes(repo_rel_path, data, message)

    def publish_many_files(
        self,
        items: Iterable[Tuple[Path, str]],
        message: str = "publish",
        throttle_every: int = 50,
        sleep_secs: float = 0.5,
    ) -> None:
        """
        Publish many local files. items: iterable of (local_path, repo_rel_path)
        """
        for i, (local, rel) in enumerate(items, 1):
            self.publish_file(local, rel, message)
            if throttle_every and (i % throttle_every == 0):
                time.sleep(sleep_secs)









