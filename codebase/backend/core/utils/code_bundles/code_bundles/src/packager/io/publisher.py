from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Dict, Any
import base64, json, time

try:
    import requests
except Exception:  # light fallback instruction if requests isn't installed
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
    def __init__(self, root: Path, clean_before_publish: bool = False) -> None:
        self.root = root
        self.clean_before_publish = clean_before_publish

    def publish(self, items: List[PublishItem]) -> None:
        if self.clean_before_publish and self.root.exists():
            # Best-effort wipe
            for p in sorted(self.root.rglob("*"), reverse=True):
                try:
                    if p.is_file() or p.is_symlink():
                        p.unlink()
                    elif p.is_dir():
                        try:
                            p.rmdir()
                        except OSError:
                            pass
                except Exception:
                    pass

        self.root.mkdir(parents=True, exist_ok=True)
        for it in items:
            p = self.root / it.path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(it.data)


class GitHubPublisher(Publisher):
    """
    Minimal GitHub Contents API client: PUT /repos/{owner}/{repo}/contents/{path}
    Strategy:
      - Try PUT *without* sha (assume create/replace). If server demands sha (422/409),
        fetch sha once and retry PUT with sha.
      - Robust retries/backoff on 429/5xx.
    """
    def __init__(self, *, owner: str, repo: str, branch: str, base_path: str, token: str,
                 clean_before_publish: bool = False) -> None:
        if requests is None:
            raise RuntimeError("The 'requests' package is required for GitHub publishing.")
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.base_path = base_path.strip("/")
        self.clean_before_publish = clean_before_publish
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "code-bundles-packager/1.0"
        })
        self.api = f"https://api.github.com/repos/{self.owner}/{self.repo}"

    def _contents_url(self, rel: str) -> str:
        return f"{self.api}/contents/{rel}"

    def _full_repo_path(self, rel: str) -> str:
        rel = rel.replace("\\", "/").lstrip("./")
        return f"{self.base_path}/{rel}".strip("/") if self.base_path else rel

    # --------- low-level request with retries ----------
    def _request(self, method: str, url: str, *, max_tries: int = 5, backoff_base: float = 0.5, **kwargs) -> "requests.Response":
        last = None
        for i in range(max_tries):
            r = self.session.request(method, url, timeout=30, **kwargs)
            if r.status_code in (429, 500, 502, 503, 504):
                last = r
                time.sleep(backoff_base * (2 ** i))
                continue
            return r
        return last if last is not None else self.session.request(method, url, timeout=30, **kwargs)

    def _get_sha(self, rel: str) -> Optional[str]:
        url = self._contents_url(rel)
        r = self._request("GET", url, params={"ref": self.branch})
        if r.status_code == 200:
            try:
                return r.json().get("sha")
            except Exception:
                return None
        if r.status_code in (404, 429, 500, 502, 503, 504):
            # Treat as "unknown" (we'll try PUT and let the server guide us)
            return None
        raise RuntimeError(f"GitHub GET {rel} failed {r.status_code}: {r.text[:200]}")

    def _put_file(self, rel: str, content_b64: str, sha: Optional[str]) -> "requests.Response":
        url = self._contents_url(rel)
        body = {
            "message": f"packager: update {rel}",
            "content": content_b64,
            "branch": self.branch
        }
        if sha:
            body["sha"] = sha
        return self._request("PUT", url, data=json.dumps(body))

    def publish(self, items: List[PublishItem]) -> None:
        # NOTE: A full tree reset requires Git Data API; for now, we rely on
        #       consumers enabling clean_before_publish + dedicated base_path.
        #       (Directory deletion via Contents API is per-file and expensive.)
        for it in items:
            rel = self._full_repo_path(it.path)
            content_b64 = base64.b64encode(it.data).decode("ascii")

            # Try PUT without sha first (create or replace); if server complains, fetch sha and retry.
            r = self._put_file(rel, content_b64, sha=None)
            if r.status_code in (200, 201):
                continue

            if r.status_code in (409, 422):
                # sha required / conflict — fetch current sha then retry once
                sha = self._get_sha(rel)
                r2 = self._put_file(rel, content_b64, sha=sha)
                if r2.status_code in (200, 201):
                    continue
                raise RuntimeError(f"GitHub PUT {rel} (with sha) failed {r2.status_code}: {r2.text[:200]}")

            # Other failures
            raise RuntimeError(f"GitHub PUT {rel} failed {r.status_code}: {r.text[:200]}")

