from __future__ import annotations
import time
import json
from pathlib import Path
from types import SimpleNamespace as NS
from urllib import error, parse, request
from typing import List, Optional, Tuple

from v2.backend.core.utils.code_bundles.code_bundles.src.packager.io.publisher import GitHubPublisher, GitHubTarget

from v2.backend.core.utils.code_bundles.code_bundles.execute.loader import (
    ConfigError,
)
from v2.backend.core.utils.code_bundles.code_bundles.execute.funcs import (
    is_managed_path
)


# ──────────────────────────────────────────────────────────────────────────────
# GitHub helpers
# ──────────────────────────────────────────────────────────────────────────────

def _cfg_req(mapping_or_ns, key: str, label: str = "config"):
    if isinstance(mapping_or_ns, dict):
        val = mapping_or_ns.get(key, None)
    else:
        val = getattr(mapping_or_ns, key, None)
    if val is None:
        raise ConfigError(f"Missing required config: {label}.{key}")
    return val

def _cfg_get(mapping_or_ns, key: str, default=None):
    try:
        return mapping_or_ns.get(key, default)
    except AttributeError:
        return getattr(mapping_or_ns, key, default)


# Sensible defaults when loader does not carry these fields through
DEFAULTS = NS(
    api_base='https://api.github.com',
    user_agent='code-bundles-packager',
    timeout=30,
    long_timeout=60,
    throttle_every=50,
    sleep_secs=0.25,
    raw_base='https://raw.githubusercontent.com',
)


def gh_headers(token: str, user_agent: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": user_agent,
        "Content-Type": "application/json; charset=utf-8",
    }

def gh_json(url: str, token: str, *, timeout: int, user_agent: str):
    req = request.Request(url, headers=gh_headers(token, user_agent))
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def gh_delete_file(
    owner: str,
    repo: str,
    path: str,
    sha: str,
    branch: str,
    token: str,
    msg: str,
    *, api_base: str, timeout: int, user_agent: str) -> None:
    url = f"{api_base}/repos/{owner}/{repo}/contents/{parse.quote(path)}"
    body = json.dumps({"message": msg, "sha": sha, "branch": branch}).encode("utf-8")
    req = request.Request(url, data=body, headers=gh_headers(token, user_agent), method="DELETE")
    with request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def gh_list_dir(
    owner: str,
    repo: str,
    path: str,
    branch: str,
    token: str,
    *, api_base: str, timeout: int, user_agent: str):
    base = f"{api_base}/repos/{owner}/{repo}/contents"
    if path:
        url = f"{base}/{parse.quote(path)}?ref={parse.quote(branch)}"
    else:
        url = f"{base}?ref={parse.quote(branch)}"
    try:
        data = gh_json(url, token, timeout=timeout, user_agent=user_agent)
    except error.HTTPError as e:
        if e.code == 404:
            return []
        raise
    return data


def gh_walk_files(
    owner: str,
    repo: str,
    path: str,
    branch: str,
    token: str,
    *, api_base: str, timeout: int, user_agent: str):
    stack = [path]
    seen = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        items = gh_list_dir(owner, repo, cur, branch, token, api_base=api_base, timeout=timeout, user_agent=user_agent)
        if isinstance(items, dict) and items.get("type") == "file":
            yield {"path": items["path"], "sha": items["sha"]}
        elif isinstance(items, list):
            for it in items:
                if it.get("type") == "file":
                    yield {"path": it["path"], "sha": it["sha"]}
                elif it.get("type") == "dir":
                    stack.append(it["path"])


def github_clean_remote_repo(
    *,
    owner: str,
    repo: str,
    branch: str,
    base_path: str,
    token: str,
    api_base: str = "https://api.github.com",
    timeout_s: int = 30,
    user_agent: str = "code-bundles-packager",
) -> None:
    root = (base_path or "").strip("/")

    print(
        f"[packager] Publish(GitHub): cleaning remote repo "
        f"(owner={owner} repo={repo} branch={branch} base='{root or '/'}')"
    )

    files = list(gh_walk_files(owner, repo, root, branch, token, api_base=api_base, timeout=timeout_s, user_agent=user_agent))
    if not files:
        print("[packager] Publish(GitHub): remote clean - nothing to delete")
        return

    deleted = 0
    for i, f in enumerate(sorted(files, key=lambda x: x["path"])):
        try:
            gh_delete_file(owner, repo, f["path"], f["sha"], branch, token, "repo clean before publish",
                           api_base=api_base, timeout=timeout_s, user_agent=user_agent)
            deleted += 1
            if i and (i % 50 == 0):
                time.sleep(0.5)
        except Exception as e:
            print(f"[packager] Publish(GitHub): failed delete '{f['path']}': {type(e).__name__}: {e}")
    print(f"[packager] Publish(GitHub): removed {deleted}/{len(files)} remote files")


def print_full_raw_links(owner: str, repo: str, branch: str, token: str, *, raw_base: str) -> None:
    print("\n=== Raw GitHub Links (full repo) ===")
    all_files = list(gh_walk_files(owner, repo, '', branch, token, api_base=DEFAULTS.api_base, timeout=DEFAULTS.timeout, user_agent=DEFAULTS.user_agent))
    for it in sorted(all_files, key=lambda d: d["path"]):
        url = f"{raw_base}/{owner}/{repo}/{branch}/{it['path']}"
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

    # Resolve config-driven knobs (with safe defaults)
    mp = cfg.manifest_paths
    root_dir = _cfg_get(mp, 'root_dir')
    dest_dir = Path(root_dir).name

    analysis_subdir = _cfg_get(cfg.manifest_paths, 'analysis_subdir', 'analysis')
    handoff_name = _cfg_get(cfg.publish, 'handoff_filename', 'assistant_handoff.v1.json')
    runspec_name = _cfg_get(cfg.publish, 'runspec_filename', 'superbundle.run.json')
    local_sums_name = _cfg_get(cfg.manifest_paths, 'checksums_filename', 'SHA256SUMS')
    events_name = _cfg_get(cfg.manifest_paths, 'events_filename', 'events.jsonl')
    parts_index_name = (
        getattr(getattr(cfg, "transport", NS()), "parts_index_name")
        or getattr(cfg.manifest_paths, "parts_index_filename")
    )
    part_stem = str(_cfg_get(getattr(cfg, 'transport', {}), 'part_stem', 'design_manifest'))
    part_ext = str(_cfg_get(getattr(cfg, 'transport', {}), 'part_ext', '.txt'))
    throttle_n = int(_cfg_get(getattr(cfg.publish, 'github', {}), 'throttle_every', DEFAULTS.throttle_every))
    throttle_slp = float(_cfg_get(getattr(cfg.publish, 'github', {}), 'sleep_secs', DEFAULTS.sleep_secs))
    api_base = str(_cfg_get(getattr(cfg.publish, 'github', {}), 'api_base', DEFAULTS.api_base))
    timeout_s = int(_cfg_get(getattr(cfg.publish, 'github', {}), 'timeout', DEFAULTS.timeout))
    user_agent = str(_cfg_get(getattr(cfg.publish, 'github', {}), 'user_agent', DEFAULTS.user_agent))

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
                owner=gh.owner, repo=gh.repo, branch=gh.branch, token=token, base_path=dest_dir,
                api_base=api_base, timeout_s=timeout_s, user_agent=user_agent
            )
        except Exception as e:
            print(f"[packager] WARN: remote clean failed: {type(e).__name__}: {e}")

    # Code files
    print(f"[packager] Publish(GitHub): code files: {len(code_payload)} to base_path='{base_prefix or '/'}'")
    pub.publish_many_files(code_payload, message="publish: code snapshot", throttle_every=throttle_n, sleep_secs=throttle_slp)

    # Artifacts (always under repo-root/{dest_dir})
    art_dir = Path(cfg.out_bundle).parent
    candidates: List[Tuple[Path, str]] = []

    manifest_path = Path(manifest_override) if manifest_override else Path(cfg.out_bundle)
    sums_path = Path(sums_override) if sums_override else Path(cfg.out_sums)

    for name in (handoff_name, runspec_name, local_sums_name):
        p = art_dir / name if name != local_sums_name else sums_path
        if p.exists() and p.is_file():
            candidates.append((p, f"{dest_dir}/{p.name}"))

    if manifest_path.exists():
        candidates.append((manifest_path, f"{dest_dir}/{manifest_path.name}"))
    ev = art_dir / events_name
    if ev.exists():
        candidates.append((ev, f"{dest_dir}/{ev.name}"))

    # Parts + parts index
    idx = art_dir / str(parts_index_name)
    if idx.exists():
        candidates.append((idx, f"{dest_dir}/{idx.name}"))
    for p in sorted(art_dir.glob(f"{part_stem}*{part_ext}")):
        if p.is_file():
            candidates.append((p, f"{dest_dir}/{p.name}"))

    # Include analysis/** if enabled and present
    if bool(getattr(cfg.publish, "publish_analysis", False)):
        analysis_dir = art_dir / analysis_subdir
        if analysis_dir.exists():
            for p in analysis_dir.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(art_dir).as_posix()  # e.g., "analysis/..."
                    candidates.append((p, f"{dest_dir}/{rel}"))
        else:
            print("[packager] Publish(GitHub): analysis/ not present (skipping)")

    if not candidates:
        print("[packager] Publish(GitHub): nothing to publish in artifacts")
        return

    print(f"[packager] Publish(GitHub): artifacts: {len(candidates)}")
    pub.publish_many_files(candidates, message="publish: design manifest", throttle_every=throttle_n, sleep_secs=throttle_slp)


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
    # Resolve config driven values
    dest_dir = Path(_cfg_get(cfg.manifest_paths, 'root_dir', 'design_manifest')).name
    api_base = str(_cfg_get(getattr(cfg.publish, 'github', {}), 'api_base', DEFAULTS.api_base))
    timeout_s = int(_cfg_get(getattr(cfg.publish, 'github', {}), 'timeout', DEFAULTS.timeout))
    user_agent = str(_cfg_get(getattr(cfg.publish, 'github', {}), 'user_agent', DEFAULTS.user_agent))

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
    remote_files = list(gh_walk_files(gh_owner, gh_repo, base_prefix, gh_branch, token,
                                      api_base=api_base, timeout=timeout_s, user_agent=user_agent))
    to_delete = []
    for it in remote_files:
        path = it["path"]
        # Never touch artifacts subtree here
        if path.startswith(dest_dir + "/"):
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
            gh_delete_file(gh_owner, gh_repo, it["path"], it["sha"], gh_branch, token, "remove stale file (code)",
                           api_base=api_base, timeout=timeout_s, user_agent=user_agent)
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
    dest_dir = Path(_cfg_get(cfg.manifest_paths, 'root_dir', 'design_manifest')).name
    api_base = str(_cfg_get(getattr(cfg.publish, 'github', {}), 'api_base', DEFAULTS.api_base))
    timeout_s = int(_cfg_get(getattr(cfg.publish, 'github', {}), 'timeout', DEFAULTS.timeout))
    user_agent = str(_cfg_get(getattr(cfg.publish, 'github', {}), 'user_agent', DEFAULTS.user_agent))

    art_dir = Path(cfg.out_bundle).parent
    # Build local set including nested analysis/**
    local_rel = set()
    if art_dir.exists():
        for p in art_dir.rglob("*"):
            if p.is_file():
                local_rel.add(p.relative_to(art_dir).as_posix())  # e.g., "analysis/foo.json"
    # Remote files under {dest_dir}/**
    remote = list(gh_walk_files(gh_owner, gh_repo, dest_dir, gh_branch, token,
                                api_base=api_base, timeout=timeout_s, user_agent=user_agent))
    to_delete = []
    for it in remote:
        # it["path"] is like "{dest_dir}/..." — compare the tail
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
            gh_delete_file(gh_owner, gh_repo, it["path"], it["sha"], gh_branch, token, "remove stale file (artifacts)",
                           api_base=api_base, timeout=timeout_s, user_agent=user_agent)
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
    mp = cfg.manifest_paths
    root_dir = mp["root_dir"] if isinstance(mp, dict) else getattr(mp, "root_dir")
    dest_dir = Path(root_dir).name

    # assume _cfg_get / _cfg_req are defined (dict or namespace safe)
    tr = getattr(cfg, "transport", {})
    mp = cfg.manifest_paths
    pub = getattr(cfg, "publish", {})
    gh = _cfg_get(pub, 'github', {})  # resolves publish.github

    parts_index_name = str(_cfg_get(tr, "parts_index_name",
                                    _cfg_req(mp, "parts_index_filename", "manifest_paths")))
    split_bytes = int(_cfg_get(tr, "split_bytes", 150000) or 150000)
    part_stem = str(_cfg_req(tr, "part_stem", "transport"))
    part_ext = str(_cfg_req(tr, "part_ext", "transport"))
    gh_sums_name = str(_cfg_req(mp, "github_checksums_filename", "manifest_paths"))

    api_base = str(_cfg_get(gh, 'api_base', DEFAULTS.api_base))
    long_timeout = int(_cfg_get(gh, 'long_timeout', DEFAULTS.long_timeout))
    user_agent = str(_cfg_get(gh, 'user_agent', DEFAULTS.user_agent))

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

    # --- prepare paths under {dest_dir}/ (respect optional base_path) ---
    dest_root = f"{base_path}/{dest_dir}" if base_path else dest_dir
    files_to_commit: List[Tuple[str, bytes]] = []
    files_to_commit.append((f"{dest_root}/{parts_index_name}", parts_index_bytes))
    files_to_commit.append((f"{dest_root}/{gh_sums_name}", sums_bytes))
    for n, d in parts:
        files_to_commit.append((f"{dest_root}/{n}", d))

    # --- Git Data API single-commit publish (tree API) ---
    def _api_headers(tok: str) -> dict:
        return {
            "Authorization": f"token {tok}",
            "Accept": "application/vnd.github+json",
            "User-Agent": user_agent,
            "Content-Type": "application/json",
        }

    def _req(method: str, path: str, payload: Optional[dict] = None) -> dict:
        url = f"{api_base}/repos/{owner}/{repo}{path}"
        data = None
        headers = _api_headers(token)
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=long_timeout) as resp:
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





