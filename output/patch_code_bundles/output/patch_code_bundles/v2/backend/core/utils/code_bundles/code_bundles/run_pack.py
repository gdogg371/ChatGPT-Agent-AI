# File: v2/backend/core/utils/code_bundles/code_bundles/run_pack.py
"""
Packager runner (direct-source; no staging). Platform-agnostic (uses pathlib).

- Scans the repo directly (no staging).
- Clears output/patch_code_bundles before writing artifacts.
- Always writes manifest + SHA256SUMS (handled in orchestrator).
- Publishes to GitHub in mode: github|both.

Robust token sourcing (first non-empty wins):
  publish.github_token
  publish.github.token
  pack.publish.github_token     # if loader has already merged it
  secrets.github_token          # from get_secrets(ConfigPaths.detect())
  env: GITHUB_TOKEN / GH_TOKEN

Loads publish.local.json overrides from common locations, including:
  <repo_root>/secrets_management/publish.local.json      # ← your path
  <repo_root>/secret_management/publish.local.json       # singular variant
  <repo_root>/publish.local.json
  <repo_root>/config/publish.local.json
  <repo_root>/v2/publish.local.json
  <repo_root>/v2/config/publish.local.json
  <repo_root>/v2/backend/config/publish.local.json
  <repo_root>/v2/backend/core/config/publish.local.json
  alongside this script: run_pack.py sibling publish.local.json

No secrets are logged; only the token SOURCE label is printed.
"""
from __future__ import annotations

import os
import fnmatch
import inspect
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional, Tuple
from urllib import error, parse, request

# Ensure we import the LOCAL packager from ./src (force it ahead of site-packages)
ROOT = Path(__file__).parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Local packager (always prefer embedded implementation)
from packager.core.orchestrator import Packager
import packager.core.orchestrator as orch_mod  # for provenance printing
from packager.io.publisher import GitHubPublisher, GitHubTarget  # GitHub publishing

from v2.backend.core.configuration.loader import (
    get_repo_root,
    get_packager,
    get_secrets,
    ConfigError,
    ConfigPaths,
)


class Transport(NS):
    pass


# ---------------------------------------------------------------------------
# Filters & helpers
# ---------------------------------------------------------------------------

def _is_excluded(rel: str, exclude_globs: List[str], segment_excludes: List[str]) -> bool:
    """Mirror packager filters for URL printing & GitHub code publishing (no allow-list holes)."""
    # glob-based excludes
    for pat in exclude_globs or []:
        if fnmatch.fnmatch(rel, pat):
            return True

    # segment-based excludes
    parts = set(Path(rel).parts)
    segs = set(segment_excludes or [])
    if any(seg in parts for seg in segs):
        return True

    return False


def gather_emitted_paths(
    src_root: Path,
    emitted_prefix: str,
    *,
    exclude_globs: List[str] | None = None,
    segment_excludes: List[str] | None = None,
) -> List[Tuple[Path, str]]:
    """
    Return [(local_path, emitted_repo_rel_path)] for files under src_root,
    filtered like the packager. Emits repo-relative paths prefixed by emitted_prefix.
    """
    prefix = emitted_prefix if emitted_prefix.endswith("/") else (emitted_prefix + "/")
    out: List[Tuple[Path, str]] = []
    for p in sorted(src_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src_root).as_posix()
        if rel.startswith("codebase/"):
            rel = rel[len("codebase/"):]
        if _is_excluded(rel, exclude_globs or [], segment_excludes or []):
            continue
        out.append((p, f"{prefix}{rel}"))
    return out


def print_github_raw_urls(owner: str, repo: str, branch: str, base_path: str, paths: List[str]) -> None:
    """Print raw.githubusercontent.com URLs for each emitted path.
    Example base: https://raw.githubusercontent.com/{owner}/{repo}/refs/heads/{branch}/
    """
    base = f"https://raw.githubusercontent.com/{owner}/{repo}/refs/heads/{branch}/"
    prefix = (base_path.strip("/") + "/") if base_path else ""
    for p in paths:
        p_rel = p.lstrip("/")
        print(base + prefix + p_rel)


# --------------------------- GitHub Remote Wipe -------------------------------

def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "code-bundles-packager",
        "Content-Type": "application/json; charset=utf-8",
    }


def _gh_json(url: str, token: str):
    req = request.Request(url, headers=_gh_headers(token))
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _gh_delete_file(owner: str, repo: str, path: str, sha: str, branch: str, token: str, msg: str) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{parse.quote(path)}"
    body = json.dumps({"message": msg, "sha": sha, "branch": branch}).encode("utf-8")
    req = request.Request(url, data=body, headers=_gh_headers(token), method="DELETE")
    with request.urlopen(req, timeout=30) as resp:
        resp.read()  # drain


def _gh_list_dir(owner: str, repo: str, path: str, branch: str, token: str):
    base = f"https://api.github.com/repos/{owner}/{repo}/contents"
    if path:
        url = f"{base}/{parse.quote(path)}?ref={parse.quote(branch)}"
    else:
        url = f"{base}?ref={parse.quote(branch)}"
    try:
        data = _gh_json(url, token)
    except error.HTTPError as e:
        if e.code == 404:
            return []
        raise
    return data


def _gh_walk_files(owner: str, repo: str, path: str, branch: str, token: str):
    """Yield dicts with 'path' and 'sha' for every file under path (recursive)."""
    stack = [path]
    while stack:
        cur = stack.pop()
        items = _gh_list_dir(owner, repo, cur, branch, token)
        if isinstance(items, dict) and items.get("type") == "file":
            yield {"path": items["path"], "sha": items["sha"]}
        elif isinstance(items, list):
            for it in items:
                if it.get("type") == "file":
                    yield {"path": it["path"], "sha": it["sha"]}
                elif it.get("type") == "dir":
                    stack.append(it["path"])


def github_clean_remote_repo(*, owner: str, repo: str, branch: str, base_path: str, token: str) -> None:
    """Recursively delete ALL files under base_path ('' means repo root) on GitHub."""
    root = base_path.strip("/")
    print(
        f"[packager] Publish(GitHub): cleaning remote repo "
        f"(owner={owner} repo={repo} branch={branch} base='{root or '/'}')"
    )
    # Enumerate first to avoid deleting while walking
    files = list(_gh_walk_files(owner, repo, root, branch, token))
    if not files:
        print("[packager] Publish(GitHub): remote clean - nothing to delete")
        return

    deleted = 0
    for i, f in enumerate(sorted(files, key=lambda x: x["path"])):
        try:
            _gh_delete_file(owner, repo, f["path"], f["sha"], branch, token, "repo clean before publish")
            deleted += 1
            # tiny delay to play nice with API burst limits
            if i and (i % 50 == 0):
                time.sleep(0.5)
        except Exception as e:
            print(f"[packager] Publish(GitHub): failed delete '{f['path']}': {type(e).__name__}: {e}")
    print(f"[packager] Publish(GitHub): removed {deleted}/{len(files)} remote files")


# ---------------------------------------------------------------------------
# Config builder (shared by CLI and any Spine provider)
# ---------------------------------------------------------------------------

def build_cfg(
    *,
    src: Path,
    out: Path,
    publish_mode: str = "local",
    gh_owner: Optional[str] = None,
    gh_repo: Optional[str] = None,
    gh_branch: str = "main",
    gh_base: str = "",
    gh_token: Optional[str] = None,
    publish_codebase: bool = True,
    publish_analysis: bool = False,
    publish_handoff: bool = True,
    publish_transport: bool = True,
    local_publish_root: Optional[Path] = None,
    clean_before_publish: bool = True,
) -> NS:
    """
    Construct the Packager config namespace identical to what the CLI uses.
    """
    pack = get_packager()
    repo_root = get_repo_root()

    # Resolve outputs under the caller-supplied 'out' directory
    out_bundle = (out / "design_manifest.jsonl").resolve()
    out_runspec = (out / "superbundle.run.json").resolve()
    out_guide = (out / "assistant_handoff.v1.json").resolve()
    out_sums = (out / "design_manifest.SHA256SUMS").resolve()

    # Transport constants
    transport = Transport(
        chunk_bytes=64000,
        chunk_records=True,
        group_dirs=True,
        dir_suffix_width=2,
        parts_per_dir=10,
        part_ext=".txt",
        part_stem="design_manifest",
        parts_index_name="design_manifest_parts_index.json",
        split_bytes=300000,
        transport_as_text=True,
        preserve_monolith=False,
    )

    gh = None
    mode = (publish_mode or "local").strip().lower()
    if mode in ("github", "both"):
        gh = NS(
            owner=gh_owner or "",
            repo=gh_repo or "",
            branch=gh_branch or "main",
            base_path=gh_base or "",
        )

    # Lazy fallback only if gh_token not provided AND mode requires it
    if gh_token is None and mode in ("github", "both"):
        secrets = get_secrets(ConfigPaths.detect())
        gh_token = getattr(secrets, "github_token", "") or ""

    publish = NS(
        mode=mode,
        publish_codebase=bool(publish_codebase),
        publish_analysis=bool(publish_analysis),
        publish_handoff=bool(publish_handoff),
        publish_transport=bool(publish_transport),
        github=gh,
        github_token=(gh_token or ""),
        local_publish_root=(local_publish_root.resolve() if local_publish_root else None),
        clean_before_publish=bool(clean_before_publish),
    )

    # Effective src_dir: scan the repo directly (no staging).
    src_dir = src

    cfg = NS(
        # discovery
        source_root=src_dir,
        emitted_prefix=getattr(pack, "emitted_prefix", "output/patch_code_bundles"),
        include_globs=list(getattr(pack, "include_globs", ["**/*"])),
        exclude_globs=list(getattr(pack, "exclude_globs", [])),
        follow_symlinks=False,
        case_insensitive=False,
        segment_excludes=list(getattr(pack, "segment_excludes", [])),

        # outputs
        out_bundle=out_bundle,
        out_runspec=out_runspec,
        out_guide=out_guide,
        out_sums=out_sums,

        # features
        transport=transport,
        publish=publish,

        prompts=None,
        prompt_mode="none",
    )

    out.mkdir(parents=True, exist_ok=True)
    (repo_root / "").exists()
    return cfg


# ---------------------------------------------------------------------------
# Publishing & main
# ---------------------------------------------------------------------------

def _clear_dir_contents(root: Path) -> None:
    """Best-effort clear of directory contents while keeping the folder."""
    root.mkdir(parents=True, exist_ok=True)
    for p in sorted(root.rglob("*"), reverse=True):
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


def _publish_to_github(cfg: NS, discovered: List[Tuple[Path, str]]) -> None:
    """Push codebase + artifacts to GitHub when mode includes github."""
    gh = cfg.publish.github
    token = cfg.publish.github_token
    if not gh or not token:
        print("[packager] Publish(GitHub): skipped (no github target or token)")
        return

    target = GitHubTarget(owner=gh.owner, repo=gh.repo, branch=gh.branch, base_path=gh.base_path or "")
    pub = GitHubPublisher(target, token)

    # 1) Codebase files (under emitted_prefix/*)
    if cfg.publish.publish_codebase:
        print(f"[packager] Publish(GitHub): codebase ({len(discovered)} files)")
        items = []
        for local, emitted_rel in discovered:
            items.append((local, emitted_rel.lstrip("/")))
        pub.publish_many_files(items, message="publish: codebase")

    # 2) Artifacts: design manifest + sums + run spec + guide + parts
    artifacts: List[Tuple[Path, str]] = []
    if cfg.out_bundle and Path(cfg.out_bundle).exists():
        artifacts.append((Path(cfg.out_bundle), "design_manifest.jsonl"))
    if cfg.out_sums and Path(cfg.out_sums).exists():
        artifacts.append((Path(cfg.out_sums), "design_manifest.SHA256SUMS"))
    if cfg.out_runspec and Path(cfg.out_runspec).exists() and cfg.publish.publish_transport:
        artifacts.append((Path(cfg.out_runspec), "superbundle.run.json"))
    if cfg.out_guide and Path(cfg.out_guide).exists() and cfg.publish.publish_handoff:
        artifacts.append((Path(cfg.out_guide), "assistant_handoff.v1.json"))

    parts_dir = Path(cfg.out_bundle).parent
    part_files = sorted(parts_dir.glob(f"{cfg.transport.part_stem}*{cfg.transport.part_ext}"))
    part_index = parts_dir / cfg.transport.parts_index_name
    if cfg.publish.publish_transport and part_files:
        for pf in part_files:
            artifacts.append((pf, pf.name))
        if part_index.exists():
            artifacts.append((part_index, part_index.name))

    if artifacts:
        print(f"[packager] Publish(GitHub): artifacts ({len(artifacts)} files)")
        pub.publish_many_files(artifacts, message="publish: artifacts")

    print("[packager] Publish(GitHub): done")


def _load_publish_overrides(repo_root: Path) -> Dict[str, Any]:
    """
    Load publish.local.json from common locations (platform-agnostic).
    Returns {} if not found or unreadable.
    """
    candidates = [
        # Your secrets location(s)
        repo_root / "secrets_management" / "publish.local.json",   # plural
        repo_root / "secret_management" / "publish.local.json",    # singular (seen in excludes)
        # Common fallbacks
        repo_root / "publish.local.json",
        repo_root / "config" / "publish.local.json",
        repo_root / "v2" / "publish.local.json",
        repo_root / "v2" / "config" / "publish.local.json",
        repo_root / "v2" / "backend" / "config" / "publish.local.json",
        repo_root / "v2" / "backend" / "core" / "config" / "publish.local.json",
        # alongside this runner
        ROOT / "publish.local.json",
    ]
    for p in candidates:
        try:
            if p.exists():
                with p.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    print(f"[packager] Loaded overrides from: {p}")
                    return data
        except Exception as e:
            print(f"[packager] WARN: failed to read {p}: {type(e).__name__}: {e}")
    return {}


def _merge_publish(pub: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow merge pub with overrides; deep-merge 'github' map."""
    merged = dict(pub or {})
    if "mode" in overrides and overrides["mode"]:
        merged["mode"] = overrides["mode"]
    if "github_token" in overrides and overrides["github_token"]:
        merged["github_token"] = overrides["github_token"]

    gh = dict(merged.get("github") or {})
    og = dict(overrides.get("github") or {})
    if og:
        gh.update({k: v for k, v in og.items() if v not in (None, "")})
    merged["github"] = gh
    # Support alt nesting (overrides.github.token)
    if og.get("token"):
        # If both provided, prefer top-level github_token and warn (without printing secret)
        if "github_token" in merged and merged["github_token"]:
            print("[packager] WARN: both github_token and github.token provided; using github_token")
        else:
            merged["github_token"] = og["token"]
    return merged


def _resolve_token(pack, pub: Dict[str, Any]) -> Tuple[str, str]:
    """
    Return (token, source_label) using precedence:
    publish.github_token -> publish.github.token -> pack.publish.github_token -> secrets -> env
    """
    # 1) publish.* (overrides applied)
    t = str(pub.get("github_token") or "").strip()
    if t:
        return t, "publish.github_token"
    gh = dict(pub.get("github") or {})
    t = str(gh.get("token") or "").strip()
    if t:
        return t, "publish.github.token"

    # 2) pack.* from YAML loader (if loader may inject it)
    try:
        pack_pub = dict(getattr(pack, "publish", {}) or {})
        t = str(pack_pub.get("github_token") or "").strip()
        if t:
            return t, "pack.publish.github_token"
    except Exception:
        pass

    # 3) secrets
    try:
        secrets = get_secrets(ConfigPaths.detect())
        t = str(getattr(secrets, "github_token", "") or "").strip()
        if t:
            return t, "secrets.github_token"
    except ConfigError:
        pass

    # 4) env
    t = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    if t:
        return t, "env"

    return "", ""


def main() -> int:
    # Load pack config
    pack = get_packager()
    repo_root = get_repo_root()

    # Base publish config from YAML
    pub_yaml = dict(getattr(pack, "publish", {}) or {})

    # Overlay from publish.local.json if present (incl. secrets_management/)
    overrides = _load_publish_overrides(repo_root)
    pub = _merge_publish(pub_yaml, overrides) if overrides else pub_yaml

    mode = str(pub.get("mode", "local")).lower()
    if mode not in {"local", "github", "both"}:
        raise ConfigError("packager.yml/publish.local.json: publish.mode must be 'local', 'github', or 'both'")

    # Resolve local output root
    output_root = Path(pub.get("output_root", "output/patch_code_bundles"))
    if not output_root.is_absolute():
        output_root = (repo_root / output_root).resolve()

    # Scan source: the repo root (or set to repo_root / 'backend' if desired)
    source_root = repo_root

    github = dict(pub.get("github") or {})
    gh_owner = github.get("owner")
    gh_repo = github.get("repo")
    gh_branch = github.get("branch", "main")
    gh_base = github.get("base_path", "")

    # --- TOKEN resolution with precedence and platform-agnostic file IO
    gh_token, token_src = _resolve_token(pack, pub)

    # Fail fast if required and still missing
    if mode in {"github", "both"} and not gh_token:
        raise ConfigError(
            "GitHub mode requires a token: set publish.github_token (recommended), or publish.github.token, "
            "or secrets.github_token, or env GITHUB_TOKEN / GH_TOKEN."
        )
    if mode in {"github", "both"}:
        print(f"[packager] token source: {token_src or 'NONE'}")

    cfg = build_cfg(
        src=source_root,
        out=output_root,
        publish_mode=mode,
        gh_owner=str(gh_owner) if gh_owner else None,
        gh_repo=str(gh_repo) if gh_repo else None,
        gh_branch=str(gh_branch or "main"),
        gh_base=str(gh_base or ""),
        gh_token=str(gh_token or ""),
        publish_codebase=bool(pub.get("publish_codebase", True)),
        publish_analysis=bool(pub.get("publish_analysis", False)),
        publish_handoff=bool(pub.get("publish_handoff", True)),
        publish_transport=bool(pub.get("publish_transport", True)),
        local_publish_root=None,
        clean_before_publish=bool(pub.get("clean_before_publish", True)),
    )

    # Provenance + active filters
    print(f"[packager] using orchestrator from: {inspect.getsourcefile(orch_mod) or '?'}")
    print(f"[packager] source_root: {cfg.source_root}")
    print(f"[packager] emitted_prefix: {cfg.emitted_prefix}")
    print(f"[packager] exclude_globs: {list(cfg.exclude_globs)}")
    print(f"[packager] segment_excludes: {list(cfg.segment_excludes)}")
    print("[packager] Packager: start]")

    # Clear the local publish directory before writing new artifacts
    _clear_dir_contents(output_root)

    # Build bundle (direct-source, no ingestion)
    result = Packager(cfg, rules=None).run(external_source=None)

    print(f"Bundle: {result.out_bundle}")
    print(f"Run-spec: {result.out_runspec}")
    print(f"Guide: {result.out_guide}")

    # Prepare emitted file list for publishing
    discovered = gather_emitted_paths(
        src_root=cfg.source_root,
        emitted_prefix=cfg.emitted_prefix,
        exclude_globs=list(cfg.exclude_globs),
        segment_excludes=list(cfg.segment_excludes),
    )

    # Publish to GitHub when requested
    if cfg.publish.mode in ("github", "both"):
        _publish_to_github(cfg, discovered)

    # Print GitHub raw URLs for convenience (codebase only)
    if cfg.publish.mode in ("github", "both") and cfg.publish.github and (cfg.publish.github.branch is not None):
        emitted_repo_paths = [repo_rel for (_local, repo_rel) in discovered]
        print("[packager] GitHub Raw URLs:")
        print_github_raw_urls(
            cfg.publish.github.owner,
            cfg.publish.github.repo,
            cfg.publish.github.branch,
            cfg.publish.github.base_path or "",
            emitted_repo_paths,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_cfg", "gather_emitted_paths", "github_clean_remote_repo", "main"]





