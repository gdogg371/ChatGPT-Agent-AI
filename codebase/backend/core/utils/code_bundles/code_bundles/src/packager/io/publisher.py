# v2/backend/core/utils/code_bundles/code_bundles/src/packager/io/publisher.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, List
import base64
import json
import sys

try:
    # Keep parity with previous implementation that used requests
    import requests  # type: ignore
except Exception as e:  # pragma: no cover
    requests = None  # type: ignore


@dataclass(frozen=True)
class PublishItem:
    path: str    # repo-relative path to write (e.g., "codebase/foo.py")
    data: bytes  # file content


class Publisher:
    def publish(self, items: Iterable[PublishItem]) -> None:  # interface
        raise NotImplementedError


class LocalPublisher(Publisher):
    def __init__(self, root: Path, clean_before_publish: bool = False) -> None:
        self.root = Path(root)
        self.clean_before_publish = bool(clean_before_publish)

    def publish(self, items: Iterable[PublishItem]) -> None:
        root = self.root
        root.mkdir(parents=True, exist_ok=True)
        # NOTE: we do not “clean” by default to avoid destructive behavior
        count = 0
        for it in items:
            repo_rel = it.path.lstrip("/").replace("\\", "/")
            dest = root / repo_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(it.data)
            count += 1
        print(f"[publisher.local] wrote {count} items under '{root}'", flush=True)


class GitHubPublisher(Publisher):
    def __init__(
        self,
        *,
        owner: str,
        repo: str,
        branch: str,
        base_path: str,
        token: str,
        clean_before_publish: bool = False,
    ) -> None:
        if requests is None:
            raise RuntimeError("requests is required for GitHubPublisher but is not available.")
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.base_path = (base_path or "").strip("/")

        self.token = token
        self.clean_before_publish = bool(clean_before_publish)

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "code-bundles-packager/1.0",
        })

    # ---------------- internal helpers ----------------

    def _repo_content_url(self, repo_path: str) -> str:
        # GitHub contents API; path must be URL-encoded, but leaving simple join is fine for normal characters
        # https://docs.github.com/rest/repos/contents
        from urllib.parse import quote

        repopath = repo_path.lstrip("/").replace("\\", "/")
        return f"https://api.github.com/repos/{self.owner}/{self.repo}/contents/{quote(repopath)}"

    def _with_base(self, p: str) -> str:
        p = p.lstrip("/").replace("\\", "/")
        if self.base_path:
            return f"{self.base_path}/{p}"
        return p

    def _get_existing_sha(self, repo_path: str) -> Optional[str]:
        """
        If a file already exists at repo_path on the target branch, return its SHA.
        Returns None if it does not exist.
        Raises on unexpected HTTP errors.
        """
        url = self._repo_content_url(repo_path)
        resp = self._session.get(url, params={"ref": self.branch})
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                return None
            # If this is a file, GitHub returns a dict with 'sha'
            if isinstance(data, dict) and data.get("type") == "file":
                return data.get("sha")
            # If a directory, GitHub returns a list; not applicable here
            return None
        if resp.status_code == 404:
            return None
        # Anything else is unexpected
        raise RuntimeError(
            f"GitHub GET contents failed for {repo_path}: {resp.status_code} {resp.text}"
        )

    def _put_file(self, repo_path: str, content: bytes, sha: Optional[str]) -> None:
        """
        Create or update file contents at repo_path. Supply sha if updating.
        """
        url = self._repo_content_url(repo_path)
        payload = {
            "message": f"packager: update {repo_path}",
            "content": base64.b64encode(content).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha

        resp = self._session.put(url, data=json.dumps(payload))
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"GitHub publish failed for {repo_path}: {resp.status_code} {resp.text}"
            )

    # ---------------- public API ----------------

    def publish(self, items: Iterable[PublishItem]) -> None:
        """
        Upsert each item to GitHub. If a file exists, fetch its sha and include in PUT payload.
        """
        # OPTIONAL: clean_before_publish is intentionally ignored here due to
        # destructive nature; would require listing and deleting paths via the API.

        items_list: List[PublishItem] = list(items)
        for it in items_list:
            repo_rel = self._with_base(it.path)
            # Fetch existing sha (if file already exists)
            sha = self._get_existing_sha(repo_rel)
            # Create or update accordingly
            self._put_file(repo_rel, it.data, sha)

