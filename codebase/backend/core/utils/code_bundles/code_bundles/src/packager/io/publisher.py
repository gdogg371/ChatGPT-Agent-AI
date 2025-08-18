# v2/backend/core/utils/code_bundles/code_bundles/src/packager/io/publisher.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, List
import base64
import json
import time
import random

try:
    import requests  # type: ignore
    from requests.adapters import HTTPAdapter  # type: ignore
    from urllib3.util.retry import Retry  # type: ignore
except Exception as e:  # pragma: no cover
    requests = None  # type: ignore


@dataclass(frozen=True)
class PublishItem:
    path: str    # repo-relative path to write (e.g., "codebase/foo.py" or "assistant_handoff.v1.json")
    data: bytes  # file content


class Publisher:
    def publish(self, items: Iterable[PublishItem]) -> None:  # interface
        raise NotImplementedError


class LocalPublisher(Publisher):
    def __init__(self, root, clean_before_publish: bool = False) -> None:
        from pathlib import Path
        self.root = Path(root)
        self.clean_before_publish = bool(clean_before_publish)

    def publish(self, items: Iterable[PublishItem]) -> None:
        root = self.root
        root.mkdir(parents=True, exist_ok=True)
        count = 0
        for it in items:
            repo_rel = it.path.lstrip("/").replace("\\", "/")
            dest = root / repo_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(it.data)
            count += 1
        print(f"[publisher.local] wrote {count} items under '{root}'", flush=True)


class GitHubPublisher(Publisher):
    """
    Robust GitHub publisher:
      * Supplies existing file SHA on update (fixes 422).
      * Retries GET/PUT on connection resets and 429/5xx with backoff.
      * Small jitter between requests to avoid burst limits.
    """

    def __init__(
        self,
        *,
        owner: str,
        repo: str,
        branch: str,
        base_path: str,
        token: str,
        clean_before_publish: bool = False,
        timeout_seconds: int = 20,
        max_retries: int = 5,
        backoff_factor: float = 0.8,
    ) -> None:
        if requests is None:
            raise RuntimeError("requests is required for GitHubPublisher but is not available.")

        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.base_path = (base_path or "").strip("/")
        self.token = token
        self.clean_before_publish = bool(clean_before_publish)
        self.timeout = timeout_seconds
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "code-bundles-packager/1.0",
        })

        # Configure robust retries for GET and PUT
        retry = Retry(
            total=self.max_retries,
            connect=self.max_retries,
            read=self.max_retries,
            status=self.max_retries,
            backoff_factor=self.backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "PUT"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    # ---------------- internal helpers ----------------

    def _repo_content_url(self, repo_path: str) -> str:
        # https://docs.github.com/rest/repos/contents
        from urllib.parse import quote
        repopath = repo_path.lstrip("/").replace("\\", "/")
        return f"https://api.github.com/repos/{self.owner}/{self.repo}/contents/{quote(repopath)}"

    def _with_base(self, p: str) -> str:
        p = p.lstrip("/").replace("\\", "/")
        if self.base_path:
            return f"{self.base_path}/{p}"
        return p

    def _sleep_backoff(self, attempt: int) -> None:
        # exponential backoff with jitter
        base = (2 ** attempt) * self.backoff_factor
        time.sleep(base + random.uniform(0, 0.3))

    def _get_existing_sha(self, repo_path: str) -> Optional[str]:
        """
        If a file already exists at repo_path on the target branch, return its SHA.
        Returns None if it does not exist.
        Raises on unexpected HTTP errors (non-200/404).
        """
        url = self._repo_content_url(repo_path)

        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.get(url, params={"ref": self.branch}, timeout=self.timeout)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except Exception:
                        return None
                    if isinstance(data, dict) and data.get("type") == "file":
                        return data.get("sha")
                    return None
                if resp.status_code == 404:
                    return None
                if resp.status_code in (429, 500, 502, 503, 504):
                    if attempt < self.max_retries:
                        self._sleep_backoff(attempt)
                        continue
                # Unexpected
                raise RuntimeError(
                    f"GitHub GET contents failed for {repo_path}: {resp.status_code} {resp.text}"
                )
            except requests.exceptions.RequestException as e:
                if attempt < self.max_retries:
                    self._sleep_backoff(attempt)
                    continue
                raise RuntimeError(f"GitHub GET contents request failed for {repo_path}: {e}")

        return None

    def _put_file(self, repo_path: str, content: bytes, sha: Optional[str]) -> None:
        """
        Create or update file contents at repo_path. Supply sha if updating.
        Retries transient errors; on 422 without sha, re-fetch sha and retry once.
        """
        url = self._repo_content_url(repo_path)
        b64 = base64.b64encode(content).decode("ascii")

        def _payload(with_sha: Optional[str]) -> dict:
            payload = {
                "message": f"packager: update {repo_path}",
                "content": b64,
                "branch": self.branch,
            }
            if with_sha:
                payload["sha"] = with_sha
            return payload

        # attempt loop
        cur_sha = sha
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.put(url, data=json.dumps(_payload(cur_sha)), timeout=self.timeout)
                if resp.status_code in (200, 201):
                    return
                # 422 often means missing/incorrect sha; try fetching sha and retry once
                if resp.status_code == 422 and not cur_sha:
                    cur_sha = self._get_existing_sha(repo_path)
                    if cur_sha:
                        if attempt < self.max_retries:
                            self._sleep_backoff(attempt)
                            continue
                if resp.status_code in (429, 500, 502, 503, 504):
                    if attempt < self.max_retries:
                        self._sleep_backoff(attempt)
                        continue
                raise RuntimeError(
                    f"GitHub publish failed for {repo_path}: {resp.status_code} {resp.text}"
                )
            except requests.exceptions.RequestException as e:
                if attempt < self.max_retries:
                    self._sleep_backoff(attempt)
                    continue
                raise RuntimeError(f"GitHub PUT contents request failed for {repo_path}: {e}")

    # ---------------- public API ----------------

    def publish(self, items: Iterable[PublishItem]) -> None:
        """
        Upsert each item to GitHub. If a file exists, fetch its sha and include in PUT payload.
        """
        items_list: List[PublishItem] = list(items)

        for idx, it in enumerate(items_list, 1):
            repo_rel = self._with_base(it.path)
            # Gentle pacing to reduce chance of mid-stream disconnects
            if idx > 1:
                time.sleep(0.05)

            # Fetch existing sha (if file already exists)
            sha = self._get_existing_sha(repo_rel)
            # Create or update accordingly
            self._put_file(repo_rel, it.data, sha)


