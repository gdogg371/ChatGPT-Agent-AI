# codebase/src/packager/io/publisher.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, List
import base64
import json
import shutil

try:
    import requests  # type: ignore
except Exception:  # requests is optional at runtime; AST-safe if missing
    requests = None  # type: ignore


@dataclass(frozen=True)
class PublishItem:
    path: str   # POSIX-style path relative to mirror root
    data: bytes


class Publisher:
    def publish(self, items: Iterable[PublishItem]) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class LocalPublisher(Publisher):
    """Writes a mirror of the publish set to a local directory."""

    def __init__(self, root: Path, clean_before_publish: bool = False) -> None:
        self.root = root
        self.clean_before_publish = clean_before_publish

    def publish(self, items: Iterable[PublishItem]) -> None:
        if self.clean_before_publish and self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        count = 0
        for it in items:
            dst = (self.root / it.path).resolve()
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(it.data)
            count += 1
        print(f"[publisher.local] wrote {count} items under '{self.root}'", flush=True)


class GitHubPublisher(Publisher):
    """
    Minimal GitHub publisher using the 'contents' API per file.

    Notes:
      - This is intentionally simple and pushes files one-by-one (idempotent).
      - Requires 'repo' scope token.
      - Large / many-file commits should consider the Git Trees API; this API
        keeps the surface area small and is adequate for our current use.
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        branch: str,
        base_path: str,
        token: str,
        clean_before_publish: bool = False,  # If True, we overwrite paths; no repo-wide delete here.
    ) -> None:
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.base_path = base_path.strip("/")

        self.token = token
        self.clean_before_publish = clean_before_publish
        if requests is None:
            # Defer failure until publish() is called to keep AST import-safe
            pass

    # ---- internal helpers -----------------------------------------------------
    def _api_url(self, rel_path: str) -> str:
        p = f"{self.base_path}/{rel_path}" if self.base_path else rel_path
        return f"https://api.github.com/repos/{self.owner}/{self.repo}/contents/{p}"

    def _headers(self) -> dict:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get_sha_if_exists(self, url: str, ref: str) -> Optional[str]:
        try:
            r = requests.get(url, headers=self._headers(), params={"ref": ref}, timeout=30)  # type: ignore
            if r.status_code == 200:
                body = r.json()
                return body.get("sha")
            return None
        except Exception:
            return None

    # ---- main ----------------------------------------------------------------
    def publish(self, items: Iterable[PublishItem]) -> None:
        if requests is None:
            raise RuntimeError("GitHubPublisher requires 'requests' to be installed.")

        # Push items one-by-one using the Contents API
        pushed = 0
        for it in items:
            url = self._api_url(it.path)
            sha = self._get_sha_if_exists(url, self.branch)
            b64 = base64.b64encode(it.data).decode("ascii")
            payload = {
                "message": f"Publish {it.path}",
                "content": b64,
                "branch": self.branch,
            }
            if sha and not self.clean_before_publish:
                payload["sha"] = sha  # update existing in-place
            resp = requests.put(url, headers=self._headers(), data=json.dumps(payload), timeout=60)  # type: ignore
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"GitHub publish failed for {it.path}: {resp.status_code} {resp.text}")
            pushed += 1
        print(f"[publisher.github] pushed {pushed} items to {self.owner}/{self.repo}@{self.branch}", flush=True)





