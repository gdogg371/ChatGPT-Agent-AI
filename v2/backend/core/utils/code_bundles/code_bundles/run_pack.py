# File: v2/backend/core/utils/code_bundles/code_bundles/run_pack.py
"""
Packager runner (direct-source; no staging). Platform-agnostic (pathlib).

Local:
  - Artifacts  -> <repo_root>/output/design_manifest/
  - Code snap  -> <repo_root>/output/patch_code_bundles/

GitHub:
  - Artifacts  -> design_manifest/ at repo root
  - Code       -> repo root (repo-relative paths; no output/ prefix)

Modes:
  - local  -> write local only
  - github -> publish to GitHub only
  - both   -> do both

Token precedence:
  publish.github_token
  publish.github.token
  pack.publish.github_token
  secrets.github_token
  env: GITHUB_TOKEN / GH_TOKEN

Overrides (publish.local.json) searched in:
  <repo_root>/secrets_management/publish.local.json
  <repo_root>/secret_management/publish.local.json
  <repo_root>/publish.local.json
  <repo_root>/config/publish.local.json
  <repo_root>/v2/publish.local.json
  <repo_root>/v2/config/publish.local.json
  <repo_root>/v2/backend/config/publish.local.json
  <repo_root>/v2/backend/core/config/publish.local.json
  <this_dir>/publish.local.json
"""
from __future__ import annotations

import base64
import fnmatch
import inspect
import json
import os
import sys
import time
from datetime import datetime, timezone
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
import packager.core.orchestrator as orch_mod  # provenance printing
from packager.io.publisher import GitHubPublisher, GitHubTarget  # GitHub publishing

# Manifest enrichment imports
from v2.backend.core.utils.code_bundles.code_bundles.bundle_io import (
    ManifestAppender,
    emit_standard_artifacts,
    emit_transport_parts,
    rewrite_manifest_paths,
    write_sha256sums_for_file,
)
from v2.backend.core.utils.code_bundles.code_bundles.python_index import (
    index_python_file,
)
from v2.backend.core.utils.code_bundles.code_bundles.quality import (
    quality_for_python,
)
from v2.backend.core.utils.code_bundles.code_bundles.graphs import (
    coalesce_edges,
)
from v2.backend.core.utils.code_bundles.code_bundles.contracts import (
    build_manifest_header,
    build_bundle_summary,
)

from v2.backend.core.configuration.loader import (
    get_repo_root,
    get_packager,
    get_secrets,
    ConfigError,
    ConfigPaths,
)


class Transport(NS):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Discovery helpers (mirror packager include/exclude behavior)
# ──────────────────────────────────────────────────────────────────────────────

def _match_any(rel_posix: str, globs: List[str], case_insensitive: bool = False) -> bool:
    if not globs:
        return False
    rp = rel_posix.casefold() if case_insensitive else rel_posix
    for g in globs:
        pat = g.replace("\\", "/")
        pat = pat.case_insensitive() if False else pat  # keep explicit; no-op if not needed
        if case_insensitive:
            pat = pat.casefold()
        if fnmatch.fnmatch(rp, pat):
            return True
    return False


def _seg_excluded(parts: Tuple[str, ...], segment_excludes: List[str], case_insensitive: bool = False) -> bool:
    if not segment_excludes:
        return False
    segs = set((s.casefold() if case_insensitive else s) for s in segment_excludes)
    for seg in parts[:-1]:  # ignore filename itself
        s = seg.casefold() if case_insensitive else seg
        if s in segs:
            return True
    return False


def discover_repo_paths(
    *,
    src_root: Path,
    include_globs: List[str],
    exclude_globs: List[str],
    segment_excludes: List[str],
    case_insensitive: bool = False,
    follow_symlinks: bool = False,
) -> List[Tuple[Path, str]]:
    """
    Discover files under src_root with the same semantics as packager discovery.
    Returns a list of (local_path, repo_relative_posix).
    """
    out: List[Tuple[Path, str]] = []
    for cur, dirs, files in os.walk(src_root, followlinks=follow_symlinks):
        # prune directories based on segment excludes for performance
        pruned_dirs = []
        for d in dirs:
            try:
                parts = (Path(cur) / d).relative_to(src_root).parts
            except Exception:
                pruned_dirs.append(d)
                continue
            if _seg_excluded(parts, segment_excludes, case_insensitive):
                continue
            pruned_dirs.append(d)
        dirs[:] = pruned_dirs

        for fn in sorted(files):
            p = Path(cur) / fn
            if not p.is_file():
                continue
            rel_posix = p.relative_to(src_root).as_posix()

            # include_globs: if set, require match
            if include_globs and not _match_any(rel_posix, include_globs, case_insensitive):
                continue

            # exclude_globs
            if exclude_globs and _match_any(rel_posix, exclude_globs, case_insensitive):
                continue

            out.append((p, rel_posix))

    out.sort(key=lambda t: t[1])
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Local snapshot & utilities
# ──────────────────────────────────────────────────────────────────────────────

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


def copy_snapshot(items: List[Tuple[Path, str]], dest_root: Path) -> int:
    """
    Copy repo files to dest_root / <repo-relative>, creating parents as needed.
    Returns number of files copied.
    """
    count = 0
    for local, rel in items:
        dst = dest_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            dst.write_bytes(local.read_bytes())
            count += 1
        except Exception as e:
            print(f"[packager] WARN: copy failed {rel}: {type(e).__name__}: {e}")
    return count


# ──────────────────────────────────────────────────────────────────────────────
# GitHub helpers
# ──────────────────────────────────────────────────────────────────────────────

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
    """Recursively delete ALL files under base_path ('' means repo root) on GitHub)."""
    root = base_path.strip("/")
    print(
        f"[packager] Publish(GitHub): cleaning remote repo "
        f"(owner={owner} repo={repo} branch={branch} base='{root or '/'}')"
    )
    files = list(_gh_walk_files(owner, repo, root, branch, token))
    if not files:
        print("[packager] Publish(GitHub): remote clean - nothing to delete")
        return

    deleted = 0
    for i, f in enumerate(sorted(files, key=lambda x: x["path"])):
        try:
            _gh_delete_file(owner, repo, f["path"], f["sha"], branch, token, "repo clean before publish")
            deleted += 1
            if i and (i % 50 == 0):
                time.sleep(0.5)
        except Exception as e:
            print(f"[packager] Publish(GitHub): failed delete '{f['path']}': {type(e).__name__}: {e}")
    print(f"[packager] Publish(GitHub): removed {deleted}/{len(files)} remote files")


def _gh_put_file(owner: str, repo: str, path: str, content_bytes: bytes, branch: str, token: str, msg: str) -> None:
    """
    Create/update a file using GitHub Contents API with in-memory bytes.
    We assume the repo root was cleaned earlier, so no 'sha' is required.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{parse.quote(path)}"
    body = json.dumps({
        "message": msg,
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": branch,
    }).encode("utf-8")
    req = request.Request(url, data=body, headers=_gh_headers(token), method="PUT")
    with request.urlopen(req, timeout=30) as resp:
        resp.read()


# ──────────────────────────────────────────────────────────────────────────────
# Config builder
# ──────────────────────────────────────────────────────────────────────────────

def build_cfg(
    *,
    src: Path,
    artifact_out: Path,
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
    Construct the Packager config namespace. We point the orchestrator's outputs
    at 'artifact_out' (local artifacts folder).
    """
    pack = get_packager()
    repo_root = get_repo_root()

    # Resolve artifact outputs under artifact_out
    out_bundle = (artifact_out / "design_manifest.jsonl").resolve()
    out_runspec = (artifact_out / "superbundle.run.json").resolve()
    out_guide = (artifact_out / "assistant_handoff.v1.json").resolve()
    out_sums = (artifact_out / "design_manifest.SHA256SUMS").resolve()

    # Transport constants (unchanged)
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

    # Windows defaults: case-insensitive matching, and follow symlinks by default
    case_insensitive = True if os.name == "nt" else False
    follow_symlinks = True

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

    cfg = NS(
        # discovery
        source_root=src,
        emitted_prefix=getattr(pack, "emitted_prefix", "output/patch_code_bundles"),
        include_globs=list(getattr(pack, "include_globs", ["**/*"])),
        exclude_globs=list(getattr(pack, "exclude_globs", [])),
        follow_symlinks=follow_symlinks,
        case_insensitive=case_insensitive,
        segment_excludes=list(getattr(pack, "segment_excludes", [])),

        # artifacts (local filesystem)
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

    artifact_out.mkdir(parents=True, exist_ok=True)
    (repo_root / "").exists()
    return cfg


# ──────────────────────────────────────────────────────────────────────────────
# Publish overrides & token resolution
# ──────────────────────────────────────────────────────────────────────────────

def _load_publish_overrides(repo_root: Path) -> Dict[str, Any]:
    """
    Load publish.local.json from common locations (platform-agnostic).
    Returns {} if not found or unreadable.
    """
    candidates = [
        repo_root / "secrets_management" / "publish.local.json",   # preferred
        repo_root / "secret_management" / "publish.local.json",    # fallback
        repo_root / "publish.local.json",
        repo_root / "config" / "publish.local.json",
        repo_root / "v2" / "publish.local.json",
        repo_root / "v2" / "config" / "publish.local.json",
        repo_root / "v2" / "backend" / "config" / "publish.local.json",
        repo_root / "v2" / "backend" / "core" / "config" / "publish.local.json",
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
    if og.get("token"):
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


# ──────────────────────────────────────────────────────────────────────────────
# Manifest path rewrite (in-memory) + bytes SHA
# ──────────────────────────────────────────────────────────────────────────────

def _rewrite_manifest_paths_to_bytes(*, manifest_in: Path, emitted_prefix: str, to_mode: str) -> bytes:
    """
    Read manifest_in JSONL, rewrite path fields for to_mode ('github'|'local'), return bytes.
    Rewrites: kind in {'file','python.module','quality.metric'} -> 'path', and 'graph.edge' -> 'src_path'.
    """
    emitted_prefix = (emitted_prefix or "").strip("/")
    def to_local(p: str) -> str:
        p = p.lstrip("/")
        if emitted_prefix and not p.startswith(emitted_prefix + "/"):
            return f"{emitted_prefix}/{p}"
        return p
    def to_github(p: str) -> str:
        p = p.lstrip("/")
        if emitted_prefix and p.startswith(emitted_prefix + "/"):
            return p[len(emitted_prefix) + 1:]
        return p
    mapper = to_github if to_mode == "github" else to_local

    out_lines: List[str] = []
    with Path(manifest_in).open("r", encoding="utf-8") as src:
        for line in src:
            try:
                rec = json.loads(line)
            except Exception:
                out_lines.append(line.rstrip("\n"))
                continue
            k = rec.get("kind")
            if k in ("file", "python.module", "quality.metric"):
                if isinstance(rec.get("path"), str):
                    rec["path"] = mapper(rec["path"])
                    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            elif k == "graph.edge":
                if isinstance(rec.get("src_path"), str):
                    rec["src_path"] = mapper(rec["src_path"])
                    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            out_lines.append(line.rstrip("\n"))
    return ("\n".join(out_lines) + "\n").encode("utf-8")


def _sha256_hex_bytes(data: bytes) -> str:
    import hashlib
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


# ──────────────────────────────────────────────────────────────────────────────
# GitHub publishing (forced to repo root)
# ──────────────────────────────────────────────────────────────────────────────

def _publish_to_github(
    cfg: NS,
    code_items_repo_rel: List[Tuple[Path, str]],
    *,
    manifest_bytes: Optional[bytes] = None,  # in-memory GH variant
    sums_bytes: Optional[bytes] = None,      # in-memory GH sums
) -> None:
    """
    Push codebase + artifacts to GitHub at the REPO ROOT.
    Artifacts are written under 'design_manifest/' at the repo root.

    If manifest_bytes / sums_bytes are provided, they are uploaded directly
    (no disk files required). Other artifacts still use the publisher.
    """
    gh = cfg.publish.github
    token = cfg.publish.github_token
    if not gh or not token:
        print("[packager] Publish(GitHub): skipped (no github target or token)")
        return

    # Force repo root (ignore any configured base_path)
    target = GitHubTarget(owner=gh.owner, repo=gh.repo, branch=gh.branch, base_path="")
    pub = GitHubPublisher(target, token)

    # 1) Codebase files (repo-relative to repo root)
    if cfg.publish.publish_codebase:
        items = [(local, rel.lstrip("/")) for local, rel in code_items_repo_rel]
        print(f"[packager] Publish(GitHub): code ({len(items)} files)")
        print(f"[packager] Publish(GitHub): first 10 code paths →",
              [rel for _loc, rel in items[:10]])
        pub.publish_many_files(items, message="publish: code")

    # 2) Artifacts under 'design_manifest/' at repo root
    # 2a) manifest + sums (in-memory if provided)
    if manifest_bytes is not None:
        _gh_put_file(gh.owner, gh.repo, "design_manifest/design_manifest.jsonl", manifest_bytes, gh.branch, token, "publish: artifacts (manifest)")
    else:
        if Path(cfg.out_bundle).exists():
            pub.publish_many_files([(Path(cfg.out_bundle), "design_manifest/design_manifest.jsonl")], message="publish: artifacts")

    if sums_bytes is not None:
        _gh_put_file(gh.owner, gh.repo, "design_manifest/design_manifest.SHA256SUMS", sums_bytes, gh.branch, token, "publish: artifacts (sums)")
    else:
        if Path(cfg.out_sums).exists():
            pub.publish_many_files([(Path(cfg.out_sums), "design_manifest/design_manifest.SHA256SUMS")], message="publish: artifacts")

    # 2b) Other artifacts from disk
    other_artifacts: List[Tuple[Path, str]] = []
    if cfg.out_runspec and Path(cfg.out_runspec).exists() and cfg.publish.publish_transport:
        other_artifacts.append((Path(cfg.out_runspec), "design_manifest/superbundle.run.json"))
    if cfg.out_guide and Path(cfg.out_guide).exists() and cfg.publish.publish_handoff:
        other_artifacts.append((Path(cfg.out_guide), "design_manifest/assistant_handoff.v1.json"))

    parts_dir = Path(cfg.out_bundle).parent
    part_files = sorted(parts_dir.glob(f"{cfg.transport.part_stem}*{cfg.transport.part_ext}"))
    part_index = parts_dir / cfg.transport.parts_index_name
    if cfg.publish.publish_transport and part_files:
        for pf in part_files:
            other_artifacts.append((pf, f"design_manifest/{pf.name}"))
        if part_index.exists():
            other_artifacts.append((part_index, f"design_manifest/{part_index.name}"))

    if other_artifacts:
        print(f"[packager] Publish(GitHub): artifacts ({len(other_artifacts)} files)")
        print(f"[packager] Publish(GitHub): artifact paths →",
              [rel for _loc, rel in other_artifacts])
        pub.publish_many_files(other_artifacts, message="publish: artifacts")

    print("[packager] Publish(GitHub): done")


def print_github_raw_urls(owner: str, repo: str, branch: str, paths: List[str]) -> None:
    """Print raw.githubusercontent.com URLs for convenience (repo-root paths)."""
    base = f"https://raw.githubusercontent.com/{owner}/{repo}/refs/heads/{branch}/"
    for p in paths:
        print(base + p.lstrip("/"))


# ──────────────────────────────────────────────────────────────────────────────
# Manifest augmentation (post-pass)
# ──────────────────────────────────────────────────────────────────────────────

def _tool_versions() -> Dict[str, str]:
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    try:
        # best effort to capture packager source provenance
        packager_src = inspect.getsourcefile(orch_mod) or "unknown"
    except Exception:
        packager_src = "unknown"
    return {"python": py, "packager_orchestrator": str(packager_src)}


def augment_manifest(
    *,
    cfg: NS,
    discovered_repo: List[Tuple[Path, str]],
    mode_local: bool,
    mode_github: bool,
    path_mode: str,  # "local" | "github"
) -> None:
    """
    Append enriched records to the manifest:
      - manifest.header (if missing)
      - python.module
      - quality.metric
      - graph.edge (coalesced)
      - artifact (standard + transport parts)
      - bundle.summary

    path_mode:
      - "local": new records get paths prefixed with emitted_prefix
      - "github": new records use repo-root relative paths
    """
    app = ManifestAppender(Path(cfg.out_bundle))

    # Ensure header present (front-insert if needed)
    header = build_manifest_header(
        manifest_version="1.0",
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_root=str(cfg.source_root),
        include_globs=list(cfg.include_globs),
        exclude_globs=list(cfg.exclude_globs),
        segment_excludes=list(cfg.segment_excludes),
        case_insensitive=bool(getattr(cfg, "case_insensitive", False)),
        follow_symlinks=bool(getattr(cfg, "follow_symlinks", False)),
        modes={"local": bool(mode_local), "github": bool(mode_github)},
        tool_versions=_tool_versions(),
    )
    app.ensure_header(header)

    emitted_prefix = str(cfg.emitted_prefix).strip("/")

    def map_path(rel: str) -> str:
        rel = rel.lstrip("/")
        if path_mode == "github":
            return rel
        return f"{emitted_prefix}/{rel}" if emitted_prefix else rel

    # Analysis passes
    t0 = time.perf_counter()
    module_count = 0
    quality_count = 0
    edges_accum: List[Dict[str, Any]] = []

    # Only process .py files for index/quality
    for local, rel in discovered_repo:
        if not rel.endswith(".py"):
            continue

        # python.module + edges
        mod_rec, edges = index_python_file(
            repo_root=Path(cfg.source_root),
            local_path=local,
            repo_rel_posix=rel,
        )
        if mod_rec:
            mod_rec["path"] = map_path(rel)
            app.append_record(mod_rec)
            module_count += 1
        if edges:
            for e in edges:
                e["src_path"] = map_path(rel)
            edges_accum.extend(edges)

    t1 = time.perf_counter()

    # quality.metric
    for local, rel in discovered_repo:
        if not rel.endswith(".py"):
            continue
        qrec = quality_for_python(path=local, repo_rel_posix=rel)
        qrec["path"] = map_path(rel)
        app.append_record(qrec)
        quality_count += 1

    t2 = time.perf_counter()

    # graph.edge (coalesced)
    edges_dedup = coalesce_edges(edges_accum)
    for e in edges_dedup:
        app.append_record(e)

    t3 = time.perf_counter()

    # artifact records (manifest/sums/run/guide + optional transport parts)
    art_count = 0
    art_count += emit_standard_artifacts(
        appender=app,
        out_bundle=Path(cfg.out_bundle),
        out_sums=Path(cfg.out_sums),
        out_runspec=Path(cfg.out_runspec) if cfg.out_runspec else None,
        out_guide=Path(cfg.out_guide) if cfg.out_guide else None,
    )
    art_count += emit_transport_parts(
        appender=app,
        parts_dir=Path(cfg.out_bundle).parent,
        part_stem=str(cfg.transport.part_stem),
        part_ext=str(cfg.transport.part_ext),
        parts_index_name=str(cfg.transport.parts_index_name),
    )

    # bundle.summary
    summary = build_bundle_summary(
        counts={
            "files": len(discovered_repo),
            "modules": module_count,
            "edges": len(edges_dedup),
            "metrics": quality_count,
            "artifacts": art_count,
        },
        durations_ms={
            "index_ms": int((t1 - t0) * 1000),
            "quality_ms": int((t2 - t1) * 1000),
            "graph_ms": int((t3 - t2) * 1000),
        },
    )
    app.append_record(summary)

    print(
        f"[packager] Augment manifest: modules={module_count}, metrics={quality_count}, "
        f"edges={len(edges_dedup)}, artifacts={art_count}, path_mode={path_mode}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    pack = get_packager()
    repo_root = get_repo_root()

    # Base publish config from YAML + overrides
    pub_yaml = dict(getattr(pack, "publish", {}) or {})
    overrides = _load_publish_overrides(repo_root)
    pub = _merge_publish(pub_yaml, overrides) if overrides else pub_yaml

    mode = str(pub.get("mode", "local")).lower()
    if mode not in {"local", "github", "both"}:
        raise ConfigError("packager.yml/publish.local.json: publish.mode must be 'local', 'github', or 'both'")
    do_local = mode in {"local", "both"}
    do_github = mode in {"github", "both"}
    print(f"[packager] mode: {mode} (local={do_local}, github={do_github})")

    # Paths per requirements
    artifact_root = (repo_root / "output" / "design_manifest").resolve()
    code_output_root = (repo_root / "output" / "patch_code_bundles").resolve()

    # Source scan roots
    source_root = repo_root  # direct-source (no staging)

    github = dict(pub.get("github") or {})
    gh_owner = github.get("owner")
    gh_repo = github.get("repo")
    gh_branch = github.get("branch", "main")

    # Resolve token
    gh_token, token_src = _resolve_token(pack, pub)
    if do_github and not gh_token:
        raise ConfigError(
            "GitHub mode requires a token: set publish.github_token (recommended), or publish.github.token, "
            "or secrets.github_token, or env GITHUB_TOKEN / GH_TOKEN."
        )
    if do_github:
        print(f"[packager] token source: {token_src or 'NONE'}")

    # Build cfg for orchestrator with artifact_root
    cfg = build_cfg(
        src=source_root,
        artifact_out=artifact_root,
        publish_mode=mode,
        gh_owner=str(gh_owner) if gh_owner else None,
        gh_repo=str(gh_repo) if gh_repo else None,
        gh_branch=str(gh_branch or "main"),
        gh_base="",  # ignored by publisher; forced to repo root anyway
        gh_token=str(gh_token or ""),
        publish_codebase=bool(pub.get("publish_codebase", True)),
        publish_analysis=bool(pub.get("publish_analysis", False)),
        publish_handoff=bool(pub.get("publish_handoff", True)),
        publish_transport=bool(pub.get("publish_transport", True)),
        local_publish_root=None,
        clean_before_publish=True,  # we force clean at repo root in code below
    )

    # Provenance + active filters
    print(f"[packager] using orchestrator from: {inspect.getsourcefile(orch_mod) or '?'}")
    print(f"[packager] source_root: {cfg.source_root}")
    print(f"[packager] emitted_prefix: {cfg.emitted_prefix}")
    print(f"[packager] include_globs: {list(cfg.include_globs)}")
    print(f"[packager] exclude_globs: {list(cfg.exclude_globs)}")
    print(f"[packager] segment_excludes: {list(cfg.segment_excludes)}")
    print(f"[packager] follow_symlinks: {cfg.follow_symlinks}  case_insensitive: {cfg.case_insensitive}")
    print("[packager] Packager: start]")

    # Clear local destinations according to mode
    if do_local:
        _clear_dir_contents(artifact_root)
        _clear_dir_contents(code_output_root)

    # Run packager (writes artifacts to artifact_root ALWAYS)
    result = Packager(cfg, rules=None).run(external_source=None)
    print(f"Bundle: {result.out_bundle}")
    print(f"Run-spec: {result.out_runspec}")
    print(f"Guide: {result.out_guide}")

    # Discover repo files once (used for both local snapshot & GitHub code publish)
    discovered_repo = discover_repo_paths(
        src_root=cfg.source_root,
        include_globs=list(cfg.include_globs),
        exclude_globs=list(cfg.exclude_globs),
        segment_excludes=list(cfg.segment_excludes),
        case_insensitive=bool(getattr(cfg, "case_insensitive", False)),
        follow_symlinks=bool(getattr(cfg, "follow_symlinks", False)),
    )
    print(f"[packager] discovered repo files: {len(discovered_repo)}")

    # LOCAL: copy code snapshot + artifacts already written by orchestrator
    if do_local:
        copied = copy_snapshot(discovered_repo, code_output_root)
        print(f"[packager] Local snapshot: copied {copied} files to {code_output_root}")

    # ── PATH MODE & REWRITE STRATEGY ────────────────────────────────────────
    # We want:
    #   - local: manifest paths = local snapshot (prefix emitted_prefix)
    #   - github: manifest paths = repo-root
    #   - both: local manifest stays local; github gets a rewritten copy (in-memory)
    #
    manifest_path = Path(cfg.out_bundle)
    sums_path = Path(cfg.out_sums)
    emitted_prefix = str(cfg.emitted_prefix).strip("/")

    gh_manifest_bytes: Optional[bytes] = None
    gh_sums_bytes: Optional[bytes] = None

    if do_github and not do_local:
        # GitHub-only: rewrite any pre-existing 'file' paths to repo-root first,
        # then enrich with github-style paths, then refresh sums.
        rewrite_manifest_paths(
            manifest_in=manifest_path,
            manifest_out=manifest_path,
            emitted_prefix=emitted_prefix,
            to_mode="github",
        )
        augment_manifest(
            cfg=cfg,
            discovered_repo=discovered_repo,
            mode_local=do_local,
            mode_github=do_github,
            path_mode="github",
        )
        write_sha256sums_for_file(target_file=manifest_path, out_sums_path=sums_path)

    elif do_local and not do_github:
        # Local-only: enrich with local-style paths and refresh sums.
        augment_manifest(
            cfg=cfg,
            discovered_repo=discovered_repo,
            mode_local=do_local,
            mode_github=do_github,
            path_mode="local",
        )
        write_sha256sums_for_file(target_file=manifest_path, out_sums_path=sums_path)

    else:
        # BOTH:
        # 1) Enrich local manifest with local paths and refresh sums.
        augment_manifest(
            cfg=cfg,
            discovered_repo=discovered_repo,
            mode_local=True,
            mode_github=True,
            path_mode="local",
        )
        write_sha256sums_for_file(target_file=manifest_path, out_sums_path=sums_path)

        # 2) Build a GitHub-variant copy with repo-root paths **in memory** and its sums
        gh_manifest_bytes = _rewrite_manifest_paths_to_bytes(
            manifest_in=manifest_path,
            emitted_prefix=emitted_prefix,
            to_mode="github",
        )
        gh_digest = _sha256_hex_bytes(gh_manifest_bytes)
        gh_sums_bytes = f"{gh_digest}  design_manifest.jsonl\n".encode("utf-8")

    # GITHUB: clean root, then publish code (repo-relative) + artifacts under design_manifest/
    if do_github:
        try:
            github_clean_remote_repo(
                owner=cfg.publish.github.owner,
                repo=cfg.publish.github.repo,
                branch=cfg.publish.github.branch,
                base_path="",  # force root clean
                token=cfg.publish.github_token,
            )
        except Exception as e:
            print(f"[packager] Publish(GitHub): remote clean failed: {type(e).__name__}: {e}", file=sys.stderr)

        _publish_to_github(
            cfg,
            discovered_repo,
            manifest_bytes=gh_manifest_bytes,
            sums_bytes=gh_sums_bytes,
        )

        # Convenience: print raw URLs for code root + artifact paths
        code_repo_paths = [rel for (_local, rel) in discovered_repo]
        art_repo_paths = [
            "design_manifest/design_manifest.jsonl",
            "design_manifest/design_manifest.SHA256SUMS",
            "design_manifest/superbundle.run.json",
            "design_manifest/assistant_handoff.v1.json",
        ]
        # Add transport parts if present (published alongside manifest.jsonl)
        parts_dir = Path(cfg.out_bundle).parent
        part_files = sorted(parts_dir.glob(f"{cfg.transport.part_stem}*{cfg.transport.part_ext}"))
        part_index = parts_dir / cfg.transport.parts_index_name
        for pf in part_files:
            art_repo_paths.append(f"design_manifest/{pf.name}")
        if part_index.exists():
            art_repo_paths.append(f"design_manifest/{part_index.name}")

        print("[packager] GitHub Raw URLs (code + artifacts):")
        print_github_raw_urls(cfg.publish.github.owner, cfg.publish.github.repo, cfg.publish.github.branch, code_repo_paths + art_repo_paths)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_cfg",
    "discover_repo_paths",
    "copy_snapshot",
    "github_clean_remote_repo",
    "augment_manifest",
    "main",
]













