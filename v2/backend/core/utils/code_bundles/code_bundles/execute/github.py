from __future__ import annotations
import time
import json
from pathlib import Path
from types import SimpleNamespace as NS
from urllib import error, parse, request
from typing import List, Optional, Tuple

from v2.backend.core.utils.code_bundles.code_bundles.src.packager.io.publisher import GitHubPublisher, GitHubTarget

from v2.backend.core.configuration.loader import (
    ConfigError,
)
from v2.backend.core.utils.code_bundles.code_bundles.execute.funcs import (
is_managed_path
)


# ──────────────────────────────────────────────────────────────────────────────
# GitHub helpers
# ──────────────────────────────────────────────────────────────────────────────
def gh_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "code-bundles-packager",
        "Content-Type": "application/json; charset=utf-8",
    }


def gh_json(url: str, token: str):
    req = request.Request(url, headers=gh_headers(token))
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def gh_delete_file(owner: str, repo: str, path: str, sha: str, branch: str, token: str, msg: str) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{parse.quote(path)}"
    body = json.dumps({"message": msg, "sha": sha, "branch": branch}).encode("utf-8")
    req = request.Request(url, data=body, headers=gh_headers(token), method="DELETE")
    with request.urlopen(req, timeout=30) as resp:
        resp.read()


def gh_list_dir(owner: str, repo: str, path: str, branch: str, token: str):
    base = f"https://api.github.com/repos/{owner}/{repo}/contents"
    if path:
        url = f"{base}/{parse.quote(path)}?ref={parse.quote(branch)}"
    else:
        url = f"{base}?ref={parse.quote(branch)}"
    try:
        data = gh_json(url, token)
    except error.HTTPError as e:
        if e.code == 404:
            return []
        raise
    return data


def gh_walk_files(owner: str, repo: str, path: str, branch: str, token: str):
    stack = [path]
    seen = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        items = gh_list_dir(owner, repo, cur, branch, token)
        if isinstance(items, dict) and items.get("type") == "file":
            yield {"path": items["path"], "sha": items["sha"]}
        elif isinstance(items, list):
            for it in items:
                if it.get("type") == "file":
                    yield {"path": it["path"], "sha": it["sha"]}
                elif it.get("type") == "dir":
                    stack.append(it["path"])


def github_clean_remote_repo(*, owner: str, repo: str, branch: str, base_path: str, token: str) -> None:
    root = (base_path or "").strip("/")

    print(
        f"[packager] Publish(GitHub): cleaning remote repo "
        f"(owner={owner} repo={repo} branch={branch} base='{root or '/'}')"
    )

    files = list(gh_walk_files(owner, repo, root, branch, token))
    if not files:
        print("[packager] Publish(GitHub): remote clean - nothing to delete")
        return

    deleted = 0
    for i, f in enumerate(sorted(files, key=lambda x: x["path"])):
        try:
            gh_delete_file(owner, repo, f["path"], f["sha"], branch, token, "repo clean before publish")
            deleted += 1
            if i and (i % 50 == 0):
                time.sleep(0.5)
        except Exception as e:
            print(f"[packager] Publish(GitHub): failed delete '{f['path']}': {type(e).__name__}: {e}")
    print(f"[packager] Publish(GitHub): removed {deleted}/{len(files)} remote files")


def print_full_raw_links(owner: str, repo: str, branch: str, token: str) -> None:
    print("\n=== Raw GitHub Links (full repo) ===")
    all_files = list(gh_walk_files(owner, repo, "", branch, token))
    for it in sorted(all_files, key=lambda d: d["path"]):
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{it['path']}"
        print(url)
    print(f"=== ({len(all_files)} files) ===\n")



# ──────────────────────────────────────────────────────────────────────────────
# GitHub publishing (now includes analysis/**)
# ──────────────────────────────────────────────────────────────────────────────
def publish_to_github(
    cfg: NS,
    code_items_repo_rel: List[Tuple[Path, str]],
    *,
    base_path: str,
    manifest_override: Optional[Path] = None,
    sums_override: Optional[Path] = None,
) -> None:
    if not cfg.publish.github:
        raise ConfigError("GitHub mode requires 'publish.github' coordinates")
    gh = cfg.publish.github
    token = str(cfg.publish.github_token or "").strip()
    if not token:
        raise ConfigError("GitHub mode requires a token from secret_management/secrets.yml -> github.api_key")

    # Apply base_path for CODE
    base_prefix = (base_path or "").strip().strip("/")
    if base_prefix:
        code_payload = []
        for (local, rel) in code_items_repo_rel:
            dest = f"{base_prefix}/{rel}"
            code_payload.append((local, dest))
    else:
        code_payload = code_items_repo_rel

    target = GitHubTarget(owner=gh.owner, repo=gh.repo, branch=gh.branch, base_path="")
    pub = GitHubPublisher(target=target, token=token)

    # Optional artifacts clean (design_manifest subtree only)
    if bool(getattr(cfg.publish, "clean_before_publish", False)):
        try:
            github_clean_remote_repo(
                owner=gh.owner, repo=gh.repo, branch=gh.branch, token=token, base_path="design_manifest"
            )
        except Exception as e:
            print(f"[packager] WARN: remote clean failed: {type(e).__name__}: {e}")

    # Code files
    print(f"[packager] Publish(GitHub): code files: {len(code_payload)} to base_path='{base_prefix or '/'}'")
    pub.publish_many_files(code_payload, message="publish: code snapshot", throttle_every=50, sleep_secs=0.5)

    # Artifacts (always under repo-root/design_manifest)
    art_dir = Path(cfg.out_bundle).parent
    candidates: List[Tuple[Path, str]] = []

    manifest_path = Path(manifest_override) if manifest_override else Path(cfg.out_bundle)
    sums_path = Path(sums_override) if sums_override else Path(cfg.out_sums)

    for name in ("assistant_handoff.v1.json", "superbundle.run.json", "design_manifest.SHA256SUMS"):
        p = art_dir / name if name != "design_manifest.SHA256SUMS" else sums_path
        if p.exists() and p.is_file():
            candidates.append((p, f"design_manifest/{p.name}"))

    if manifest_path.exists():
        candidates.append((manifest_path, f"design_manifest/{manifest_path.name}"))
    ev = art_dir / "run_events.jsonl"
    if ev.exists():
        candidates.append((ev, f"design_manifest/{ev.name}"))

    # Parts + parts index
    idx = art_dir / str(getattr(cfg.transport, "parts_index_name", "design_manifest_parts_index.json"))
    if idx.exists():
        candidates.append((idx, f"design_manifest/{idx.name}"))
    part_stem = str(getattr(cfg.transport, "part_stem", "design_manifest"))
    part_ext = str(getattr(cfg.transport, "part_ext", ".txt"))
    for p in sorted(art_dir.glob(f"{part_stem}*{part_ext}")):
        if p.is_file():
            candidates.append((p, f"design_manifest/{p.name}"))

    # NEW: include analysis/** if enabled and present
    if bool(getattr(cfg.publish, "publish_analysis", False)):
        analysis_dir = art_dir / "analysis"
        if analysis_dir.exists():
            for p in analysis_dir.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(art_dir).as_posix()  # analysis/...
                    candidates.append((p, f"design_manifest/{rel}"))
        else:
            print("[packager] Publish(GitHub): analysis/ not present (skipping)")

    if not candidates:
        print("[packager] Publish(GitHub): nothing to publish in artifacts")
        return

    print(f"[packager] Publish(GitHub): artifacts: {len(candidates)}")
    pub.publish_many_files(candidates, message="publish: design manifest", throttle_every=50, sleep_secs=0.5)


def prune_remote_code_delta(
    *,
    cfg: NS,
    gh_owner: str,
    gh_repo: str,
    gh_branch: str,
    token: str,
    discovered_repo: List[Tuple[Path, str]],
    base_path: str,
) -> int:
    # Local set includes base_path prefix if set
    base_prefix = (base_path or "").strip().strip("/")
    if base_prefix:
        local_set = {f"{base_prefix}/{rel}" for (_p, rel) in discovered_repo}
    else:
        local_set = {rel for (_p, rel) in discovered_repo}

    include_globs = list(cfg.include_globs)
    exclude_globs = list(cfg.exclude_globs)
    seg_excludes = list(cfg.segment_excludes)
    casei = bool(getattr(cfg, "case_insensitive", False))

    # Remote files under base_path (or repo root if empty)
    remote_files = list(gh_walk_files(gh_owner, gh_repo, base_prefix, gh_branch, token))
    to_delete = []
    for it in remote_files:
        path = it["path"]
        # Never touch design_manifest subtree here
        if path.startswith("design_manifest/"):
            continue
        rel_for_rules = path[len(base_prefix) + 1 :] if base_prefix and path.startswith(base_prefix + "/") else path
        if not is_managed_path(rel_for_rules, include_globs, exclude_globs, seg_excludes, casei):
            continue
        if path not in local_set:
            to_delete.append(it)

    if not to_delete:
        print("[packager] Delta prune (code): nothing to delete")
        return 0

    print(f"[packager] Delta prune (code): deleting {len(to_delete)} stale files under '{base_prefix or '/'}'")
    deleted = 0
    for i, it in enumerate(sorted(to_delete, key=lambda d: d["path"])):
        try:
            gh_delete_file(gh_owner, gh_repo, it["path"], it["sha"], gh_branch, token, "remove stale file (code)")
            deleted += 1
            if i and (i % 50 == 0):
                time.sleep(0.5)
        except Exception as e:
            print(f"[packager] WARN: failed delete (code) {it['path']}: {type(e).__name__}: {e}")
    return deleted


def prune_remote_artifacts_delta(
    *,
    cfg: NS,
    gh_owner: str,
    gh_repo: str,
    gh_branch: str,
    token: str,
) -> int:
    art_dir = Path(cfg.out_bundle).parent
    # Build local set including nested analysis/**
    local_rel = set()
    if art_dir.exists():
        for p in art_dir.rglob("*"):
            if p.is_file():
                local_rel.add(p.relative_to(art_dir).as_posix())  # e.g., "analysis/foo.json"
    # Remote files under design_manifest/**
    remote = list(gh_walk_files(gh_owner, gh_repo, "design_manifest", gh_branch, token))
    to_delete = []
    for it in remote:
        # it["path"] is like "design_manifest/..." — compare the tail
        tail = "/".join(Path(it["path"]).parts[1:])
        if tail not in local_rel:
            to_delete.append(it)

    if not to_delete:
        print("[packager] Delta prune (artifacts): nothing to delete")
        return 0

    print(f"[packager] Delta prune (artifacts): deleting {len(to_delete)} stale files")
    deleted = 0
    for i, it in enumerate(sorted(to_delete, key=lambda d: d["path"])):
        try:
            gh_delete_file(gh_owner, gh_repo, it["path"], it["sha"], gh_branch, token, "remove stale file (artifacts)")
            deleted += 1
            if i and (i % 50 == 0):
                time.sleep(0.5)
        except Exception as e:
            print(f"[packager] WARN: failed delete (artifacts) {it['path']}: {type(e).__name__}: {e}")
    return deleted


# ──────────────────────────────────────────────────────────────────────────────
# NEW: Memory-only publisher for design_manifest (GitHub flavor) — no local writes
# ──────────────────────────────────────────────────────────────────────────────
def publish_github_design_manifest_memory(
    *,
    cfg: NS,
    discovered_repo: List[Tuple[Path, str]],
) -> dict:
    """
    Build the GitHub-flavor design_manifest **in memory**, split into parts,
    compute parts index + SHA256SUMS, and push them in a single commit using
    the Git Data API. Does not write any GitHub-flavor files locally.
    """
    # Local imports to avoid changing module-level imports
    import base64
    from hashlib import sha256

    # --- resolve publish config ---
    if not cfg.publish.github:
        raise ConfigError("GitHub mode requires 'publish.github' coordinates")
    gh = cfg.publish.github
    token = str(getattr(cfg, "publish").github_token or "").strip()
    if not token:
        raise ConfigError("GitHub mode requires a token from secret_management/secrets.yml -> github.api_key")

    owner = gh.owner
    repo = gh.repo
    branch = gh.branch
    base_path = (gh.base_path or "").strip("/")

    # --- derive transport params ---
    split_bytes = int(getattr(cfg.transport, "split_bytes", 150000) or 150000)
    part_stem = str(getattr(cfg.transport, "part_stem", "design_manifest"))
    part_ext = str(getattr(cfg.transport, "part_ext", ".txt"))
    parts_index_name = str(getattr(cfg.transport, "parts_index_name", "design_manifest_parts_index.json"))

    # --- load existing LOCAL monolith (if present) and rewrite paths to github-flavor in memory ---
    # --- build a full GitHub-flavor manifest in memory (parity with augment_manifest) ---
    from v2.backend.core.utils.code_bundles.code_bundles.execute.read_scanners import augment_manifest_memory

    manifest_bytes = augment_manifest_memory(
        cfg=cfg,
        discovered_repo=discovered_repo,
        mode_local=bool(getattr(cfg, "mode_local", True)),
        mode_github=True,
        path_mode="github",
    )

    # --- split into parts on line boundaries (preserve JSONL integrity) ---
    lines = manifest_bytes.splitlines(keepends=True)
    parts: List[Tuple[str, bytes]] = []
    buf: List[bytes] = []
    cur = 0
    total_idx = 0
    def _flush():
        nonlocal buf, cur, total_idx
        if not buf:
            return
        total_idx += 1
        series = (total_idx - 1) // 10
        name = f"{part_stem}_{series:02d}_{total_idx:04d}{part_ext}"
        data = b"".join(buf)
        parts.append((name, data))
        buf = []
        cur = 0

    for b in lines:
        blen = len(b)
        if cur and (cur + blen) > split_bytes:
            _flush()
        buf.append(b)
        cur += blen
    _flush()

    parts_index = {
        "record_type": "parts_index",
        "total_parts": len(parts),
        "split_bytes": split_bytes,
        "parts": [{"name": n, "size": len(d), "lines": d.count(b"\n")} for (n, d) in parts],
        "source": f"{part_stem}.github.jsonl",
    }
    parts_index_bytes = json.dumps(parts_index, ensure_ascii=False, indent=2).encode("utf-8")

    # --- compute SHA256SUMS content (index first, then parts) ---
    sums_lines = []
    sums_lines.append(f"{sha256(parts_index_bytes).hexdigest()}  {parts_index_name}\n")
    for n, d in parts:
        sums_lines.append(f"{sha256(d).hexdigest()}  {n}\n")
    sums_bytes = "".join(sums_lines).encode("utf-8")

    # --- prepare paths under design_manifest/ (respect optional base_path) ---
    dest_root = f"{base_path}/design_manifest" if base_path else "design_manifest"
    files_to_commit: List[Tuple[str, bytes]] = []
    files_to_commit.append((f"{dest_root}/{parts_index_name}", parts_index_bytes))
    files_to_commit.append((f"{dest_root}/design_manifest.github.SHA256SUMS", sums_bytes))
    for n, d in parts:
        files_to_commit.append((f"{dest_root}/{n}", d))

    # --- Git Data API single-commit publish (tree API) ---
    def _api_headers(tok: str) -> dict:
        return {
            "Authorization": f"token {tok}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "code-bundles-packager",
            "Content-Type": "application/json",
        }

    def _req(method: str, path: str, payload: Optional[dict] = None) -> dict:
        url = f"https://api.github.com/repos/{owner}/{repo}{path}"
        data = None
        headers = _api_headers(token)
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as e:
            msg = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"GitHub API {method} {path} failed: {e.code} {e.reason}\n{msg}") from None

    # get head & base tree
    ref = _req("GET", f"/git/refs/heads/{parse.quote(branch)}")
    head_sha = ref["object"]["sha"]
    commit = _req("GET", f"/git/commits/{head_sha}")
    base_tree_sha = commit["tree"]["sha"]

    # create blobs
    tree_entries = []
    for repo_path, content in files_to_commit:
        b64 = base64.b64encode(content).decode("ascii")
        blob = _req("POST", "/git/blobs", {"content": b64, "encoding": "base64"})
        tree_entries.append({"path": repo_path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    # create tree
    tree = _req("POST", "/git/trees", {"base_tree": base_tree_sha, "tree": tree_entries})
    tree_sha = tree["sha"]

    # create commit
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
    commit_msg = f"design_manifest: publish (github, memory-only)\n\n[automated] {stamp}"
    new_commit = _req("POST", "/git/commits", {"message": commit_msg, "tree": tree_sha, "parents": [head_sha]})
    new_sha = new_commit["sha"]

    # update ref
    _req("PATCH", f"/git/refs/heads/{parse.quote(branch)}", {"sha": new_sha, "force": False})

    return {"kind": "github", "decision": "memory-commit", "parts": len(parts), "commit": new_sha}
