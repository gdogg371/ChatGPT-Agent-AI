# src/packager/io/publisher.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple
import base64, json, time

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except Exception:  # light fallback if requests isn't installed
    requests = None  # type: ignore


@dataclass(frozen=True)
class PublishItem:
    """A single file to publish."""
    path: str   # repo-relative using '/'
    data: bytes


class Publisher:
    def publish(self, items: List[PublishItem]) -> None:
        raise NotImplementedError


class LocalPublisher(Publisher):
    """Writes a browsable repo layout to a local folder (mirrors GitHub layout)."""
    def __init__(self, root: Path) -> None:
        self.root = root

    def publish(self, items: List[PublishItem]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for it in items:
            p = self.root / it.path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(it.data)


class GitHubPublisher(Publisher):
    """
    GitHub Contents API client: GET/PUT/DELETE /repos/{owner}/{repo}/contents/{path}
    - clean_before_publish: if True, recursively delete files under base_path before upload.
    - Hardened with timeouts, retries, and light pacing for large payloads.
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
    ) -> None:
        if requests is None:
            raise RuntimeError("The 'requests' package is required for GitHub publishing.")

        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.base_path = base_path.strip("/")
        self.clean_before_publish = bool(clean_before_publish)

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "code-bundles-packager/1.0",
            "Connection": "keep-alive",
        })

        # Robust retries for GET/PUT/DELETE on transient errors/timeouts
        retry = Retry(
            total=6,
            connect=6,
            read=6,
            backoff_factor=1.5,  # exponential backoff
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "PUT", "DELETE"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=10, pool_connections=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.api_base = f"https://api.github.com/repos/{self.owner}/{self.repo}/contents"
        # Timeouts: (connect, read)
        self.timeout = (15, 300)  # up to 5 minutes to push large bodies
        # Light pacing between uploads to reduce timeouts
        self.pace_delay_s_small = 0.2
        self.pace_delay_s_large = 1.0
        self.large_threshold_b64 = 6_000_000  # ~6 MB encoded body

    # ------------- helpers ----------------

    def _api_url(self, rel: str) -> str:
        """Build a contents API URL; empty rel means repo root listing."""
        rel = rel.lstrip("/")
        return self.api_base if not rel else f"{self.api_base}/{rel}"

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        # Extra guard with a few explicit retries on exceptions
        attempts = 4
        last_exc = None
        for i in range(1, attempts + 1):
            try:
                return self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.exceptions.RequestException as e:
                last_exc = e
                time.sleep(min(20.0, 1.5 ** i))
        raise requests.exceptions.RequestException(f"request failed after retries: {last_exc}")  # type: ignore

    def _get_sha(self, rel: str) -> Optional[str]:
        url = self._api_url(rel)
        r = self._request("GET", url, params={"ref": self.branch})
        if r.status_code == 200:
            try:
                return r.json().get("sha")
            except Exception:
                return None
        if r.status_code == 404:
            return None
        raise RuntimeError(f"GitHub GET {rel} failed {r.status_code}: {r.text[:400]}")

    def _list_recursive_files(self, rel_dir: str) -> List[Tuple[str, str]]:
        """
        Recursively list files under rel_dir ('' means repo root).
        Returns list of (path, sha) for files only.
        """
        files: List[Tuple[str, str]] = []
        stack = [rel_dir.strip("/")]  # '' or 'some/base'
        seen = set()

        while stack:
            cur = stack.pop()
            key = cur or "<root>"
            if key in seen:
                continue
            seen.add(key)

            url = self._api_url(cur)
            r = self._request("GET", url, params={"ref": self.branch})
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                raise RuntimeError(f"GitHub list {cur or '/'} failed {r.status_code}: {r.text[:400]}")

            data = r.json()
            if isinstance(data, dict) and data.get("type") == "file":
                # single file returned (when rel_dir is a file path)
                files.append((data["path"], data.get("sha", "")))
                continue

            if not isinstance(data, list):
                continue

            for entry in data:
                typ = entry.get("type")
                if typ == "file":
                    files.append((entry["path"], entry.get("sha", "")))
                elif typ == "dir":
                    stack.append(entry["path"])
                # ignore symlinks/submodules

        return files

    def _delete_file(self, path: str, sha: str) -> None:
        url = self._api_url(path)
        body = {
            "message": f"packager: delete {path}",
            "sha": sha,
            "branch": self.branch,
        }
        r = self._request("DELETE", url, data=json.dumps(body))
        if r.status_code in (200, 204):
            return
        if r.status_code == 404:
            return  # already gone
        # try once with refreshed sha if conflict
        if r.status_code == 409:
            new_sha = self._get_sha(path)
            if new_sha:
                body["sha"] = new_sha
                r2 = self._request("DELETE", url, data=json.dumps(body))
                if r2.status_code in (200, 204, 404):
                    return
                raise RuntimeError(f"GitHub DELETE {path} (retry) failed {r2.status_code}: {r2.text[:400]}")
        raise RuntimeError(f"GitHub DELETE {path} failed {r.status_code}: {r.text[:400]}")

    def _put_file(self, rel: str, content_b64: str, sha: Optional[str]) -> requests.Response:
        url = self._api_url(rel)
        body = {
            "message": f"packager: update {rel}",
            "content": content_b64,
            "branch": self.branch,
        }
        if sha:
            body["sha"] = sha
        if len(content_b64) >= self.large_threshold_b64:
            time.sleep(self.pace_delay_s_large)
        else:
            time.sleep(self.pace_delay_s_small)
        return self._request("PUT", url, data=json.dumps(body))

    # ------------- main --------------------

    def publish(self, items: List[PublishItem]) -> None:
        # Optionally clean target path on GitHub first.
        if self.clean_before_publish:
            base = self.base_path  # '' = repo root
            # List then delete files deepest-first
            existing = self._list_recursive_files(base)
            # delete deeper paths first (longest path first)
            existing.sort(key=lambda t: len(t[0]), reverse=True)
            for path, sha in existing:
                self._delete_file(path, sha)

        # Upload all items
        for it in items:
            rel = it.path.replace("\\", "/").lstrip("./")
            if self.base_path:
                rel = f"{self.base_path}/{rel}"

            sha = self._get_sha(rel)
            content_b64 = base64.b64encode(it.data).decode("ascii")

            r = self._put_file(rel, content_b64, sha)
            if r.status_code in (200, 201):
                continue

            if r.status_code in (409, 500, 502, 503, 504):
                r2 = self._put_file(rel, content_b64, sha=None)
                if r2.status_code in (200, 201):
                    continue
                raise RuntimeError(f"GitHub PUT {rel} (retry) failed {r2.status_code}: {r2.text[:400]}")

            raise RuntimeError(f"GitHub PUT {rel} failed {r.status_code}: {r.text[:400]}")



