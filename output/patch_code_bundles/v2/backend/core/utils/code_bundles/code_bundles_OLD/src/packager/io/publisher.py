# v2/backend/core/utils/code_bundles/code_bundles/src/packager/io/publisher.py
from __future__ import annotations

import base64
import json
from typing import List, Dict, Any, Tuple, Optional
from urllib import request, parse, error


def _log(msg: str) -> None:
    print(f"[packager] {msg}", flush=True)


def _join_path(*parts: str) -> str:
    out = "/".join(s.strip("/") for s in parts if s is not None and s != "")
    return out


def _gh_contents_url(owner: str, repo: str, path: str, branch: str) -> str:
    # GET/PUT: https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}
    safe_path = parse.quote(path)
    qs = parse.urlencode({"ref": branch})
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{safe_path}?{qs}"


def _gh_headers(token: str) -> Dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "code-bundles-publisher",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _http_json(method: str, url: str, headers: Dict[str, str], payload: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers = dict(headers)
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = request.Request(url=url, method=method, headers=headers, data=data)
    try:
        with request.urlopen(req) as resp:
            body = resp.read()
            status = resp.getcode()
            try:
                return status, json.loads(body.decode("utf-8"))
            except Exception:
                return status, {}
    except error.HTTPError as e:
        try:
            body = e.read()
            msg = body.decode("utf-8", errors="replace")
        except Exception:
            msg = str(e)
        return e.code, {"error": msg}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def _get_current_sha(owner: str, repo: str, branch: str, base_path: str, rel: str, token: str) -> Optional[str]:
    remote_path = _join_path(base_path, rel)
    url = _gh_contents_url(owner, repo, remote_path, branch)
    status, js = _http_json("GET", url, _gh_headers(token))
    if status == 200 and isinstance(js, dict):
        return js.get("sha")
    return None


def _put_file(owner: str, repo: str, branch: str, base_path: str, rel: str, data: bytes, token: str, message: str) -> bool:
    remote_path = _join_path(base_path, rel)
    url = _gh_contents_url(owner, repo, remote_path, branch)
    sha = _get_current_sha(owner, repo, branch, base_path, rel, token)

    payload = {
        "message": message,
        "branch": branch,
        "content": base64.b64encode(data).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha

    status, js = _http_json("PUT", url, _gh_headers(token), payload)
    if status in (200, 201):
        return True
    _log(f"Publish(GitHub): FAILED {remote_path} → {status} {js.get('error') or js}")
    return False


def publish(cfg, records: List[Dict[str, Any]]) -> None:
    """
    Publish selected records to GitHub:
      - Raw CODE files (those whose path starts with cfg.emitted_prefix) when publish_codebase=True
      - (Optionally) you can extend to publish analysis/handoff here if desired
    """
    opts = getattr(cfg, "publish", None)
    if not opts:
        _log("Publish: no publish options in cfg → skipping")
        return

    mode = getattr(opts, "mode", None)
    if mode not in ("github", "both"):
        _log("Publish: mode is not github/both → skipping")
        return

    gh = getattr(opts, "github", None)
    token = getattr(opts, "github_token", "") or ""
    if not gh or not getattr(gh, "owner", None) or not getattr(gh, "repo", None) or not getattr(gh, "branch", None):
        _log("Publish: missing GitHub target (owner/repo/branch) → skipping")
        return
    if not token:
        _log("Publish: missing GitHub token → skipping")
        return

    base_path = getattr(gh, "base_path", "") or ""
    branch = gh.branch

    # Normalize prefix for code classification
    code_prefix = cfg.emitted_prefix if str(cfg.emitted_prefix).endswith("/") else (str(cfg.emitted_prefix) + "/")

    # --- Collect targets -----------------------------------------------------
    code_files: List[Tuple[str, bytes]] = []
    for rec in records:
        if rec.get("type") != "file":
            continue
        path = rec.get("path") or ""
        if not path:
            continue
        b64 = rec.get("content_b64")
        if not b64:
            continue
        if path.startswith(code_prefix):
            try:
                data = base64.b64decode(b64)
            except Exception:
                continue
            code_files.append((path, data))

    did_any = False

    # --- Publish code files --------------------------------------------------
    if getattr(opts, "publish_codebase", False):
        ok = 0
        for rel, data in code_files:
            msg = f"publish(code): {rel}"
            if _put_file(gh.owner, gh.repo, branch, base_path, rel, data, token, msg):
                ok += 1
        _log(f"Publish(GitHub): code files pushed = {ok}/{len(code_files)}")
        did_any = did_any or (ok > 0)
    else:
        _log("Publish(GitHub): publish_codebase=False → skipping raw code files")

    # (Optional) handoff/analysis transport could also be published here
    # by selecting matching record paths; left as-is for now.

    if not did_any:
        _log("Publish(GitHub): nothing published (filters or errors)")







