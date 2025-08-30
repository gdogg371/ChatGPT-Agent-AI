# File: v2/backend/core/utils/code_bundles/code_bundles/run_pack.py
"""
Packager runner (direct-source; no staging). Platform-agnostic (pathlib).

Local:
  - Artifacts -> /output/design_manifest/
  - Code snap -> /output/patch_code_bundles/

GitHub:
  - Artifacts -> design_manifest/ at repo root
  - Code      -> repo root (repo-relative paths; no output/ prefix)

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
  /secrets_management/publish.local.json
  /secret_management/publish.local.json
  /publish.local.json
  /config/publish.local.json
  /v2/publish.local.json
  /v2/config/publish.local.json
  /v2/backend/config/publish.local.json
  /v2/backend/core/config/publish.local.json
  ./publish.local.json
"""
from __future__ import annotations

import fnmatch
import inspect
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Dict, Iterable, List, Optional, Tuple
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

# Manifest enrichment + helpers
from v2.backend.core.utils.code_bundles.code_bundles.bundle_io import (
    ManifestAppender,
    emit_standard_artifacts,
    emit_transport_parts,
    rewrite_manifest_paths,
    write_sha256sums_for_file,
)
from v2.backend.core.utils.code_bundles.code_bundles.python_index import index_python_file
from v2.backend.core.utils.code_bundles.code_bundles.quality import quality_for_python
from v2.backend.core.utils.code_bundles.code_bundles.graphs import coalesce_edges
from v2.backend.core.utils.code_bundles.code_bundles.contracts import (
    build_manifest_header,
    build_bundle_summary,
)
# Newly wired scanners
from v2.backend.core.utils.code_bundles.code_bundles.doc_coverage import scan as scan_doc_coverage
from v2.backend.core.utils.code_bundles.code_bundles.complexity import scan as scan_complexity
from v2.backend.core.utils.code_bundles.code_bundles.owners_index import scan as scan_owners
from v2.backend.core.utils.code_bundles.code_bundles.env_index import scan as scan_env
from v2.backend.core.utils.code_bundles.code_bundles.entrypoints import scan as scan_entrypoints
from v2.backend.core.utils.code_bundles.code_bundles.html_index import scan as scan_html
from v2.backend.core.utils.code_bundles.code_bundles.sql_index import scan as scan_sql
from v2.backend.core.utils.code_bundles.code_bundles.js_ts_index import scan as scan_js_ts
from v2.backend.core.utils.code_bundles.code_bundles.deps_index import scan as scan_deps
from v2.backend.core.utils.code_bundles.code_bundles.git_info import scan as scan_git
from v2.backend.core.utils.code_bundles.code_bundles.license_scan import scan as scan_license
from v2.backend.core.utils.code_bundles.code_bundles.secrets_scan import scan as scan_secrets
from v2.backend.core.utils.code_bundles.code_bundles.assets_index import scan as scan_assets

from v2.backend.core.configuration.loader import (
    get_repo_root,
    get_packager,
    get_secrets,
    ConfigError,
    ConfigPaths,
)

# For reading code_bundle params from vars.yml
import yaml


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
        pat = pat.casefold() if case_insensitive else pat
        if fnmatch.fnmatch(rp, pat):
            return True
    return False


def _seg_excluded(
    parts: Tuple[str, ...],
    segment_excludes: List[str],
    case_insensitive: bool = False,
) -> bool:
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
    Copy repo files to dest_root/<repo-rel>, creating parents as needed.
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
    """Recursively delete ALL files under base_path ('' means repo root) on GitHub."""
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
    Construct the Packager config namespace.
    We point the orchestrator's outputs at 'artifact_out' (local artifacts folder).
    """
    pack = get_packager()
    repo_root = get_repo_root()

    # Resolve artifact outputs under artifact_out
    out_bundle = (artifact_out / "design_manifest.jsonl").resolve()
    out_runspec = (artifact_out / "superbundle.run.json").resolve()
    out_guide = (artifact_out / "assistant_handoff.v1.json").resolve()
    out_sums = (artifact_out / "design_manifest.SHA256SUMS").resolve()

    # Transport constants (unchanged defaults; concrete behavior is driven by vars.yml code_bundle)
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
    (repo_root / "").exists()  # touch
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
        repo_root / "secrets_management" / "publish.local.json",  # plural
        repo_root / "secret_management" / "publish.local.json",   # singular
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

    # 2) pack.* from YAML loader
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
# Code-bundle params from vars.yml (strictly scoped)
# ──────────────────────────────────────────────────────────────────────────────
def _read_code_bundle_params() -> Dict[str, Any]:
    """
    Read code_bundle.* from the active spine profile's vars.yml.
    We intentionally do NOT change loader.get_pipeline_vars; this is a focused read.
    Safe defaults if absent.
    """
    try:
        paths = ConfigPaths.detect()
        vars_yml = paths.spine_profile_dir / "vars.yml"
        if not vars_yml.exists():
            vars_yml = paths.spine_profile_dir / "vars.yaml"
        if vars_yml.exists():
            with vars_yml.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cb = dict((data or {}).get("code_bundle") or {})
            out = {
                "chunk_manifest": str(cb.get("chunk_manifest", "auto")).strip().lower(),
                "split_bytes": int(cb.get("split_bytes", 300000) or 300000),
                "group_dirs": bool(cb.get("group_dirs", True)),
                # passthroughs users may have: include_design_manifest, publish_github (ignored here)
            }
            # normalize enum
            if out["chunk_manifest"] not in {"auto", "always", "never"}:
                out["chunk_manifest"] = "auto"
            # hard floor
            if out["split_bytes"] < 1024:
                out["split_bytes"] = 1024
            return out
    except Exception as e:
        print(f"[packager] WARN: failed to read code_bundle params: {type(e).__name__}: {e}")
    return {"chunk_manifest": "auto", "split_bytes": 300000, "group_dirs": True}


# ──────────────────────────────────────────────────────────────────────────────
# Transport chunking (manifest -> parts)
# ──────────────────────────────────────────────────────────────────────────────
def _should_chunk(kind: str, size_bytes: int, split_bytes: int) -> bool:
    if kind == "always":
        return True
    if kind == "never":
        return False
    # auto
    return size_bytes > max(1, int(split_bytes))


def _write_parts_from_jsonl(
    *,
    src_manifest: Path,
    dest_dir: Path,
    part_stem: str,
    part_ext: str,
    split_bytes: int,
    group_dirs: bool,
    dir_suffix_width: int,
    parts_per_dir: int,
) -> Tuple[List[Path], Dict[str, Any]]:
    """
    Split a JSONL manifest into top-level parts staying <= split_bytes each.
    We write files directly into dest_dir as:
        design_manifest_0001.txt
        design_manifest_0002.txt
    If group_dirs=True, we *encode* the grouping in the filename to preserve
    compatibility with emit_transport_parts() (no recursive globbing):
        design_manifest_00_0001.txt
        design_manifest_00_0002.txt
        design_manifest_01_0011.txt
    Returns (parts_paths, index_dict).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not src_manifest.exists():
        return [], {"record_type": "parts_index", "total_parts": 0, "split_bytes": split_bytes, "parts": []}

    text = src_manifest.read_text(encoding="utf-8", errors="replace")
    lines = [ln if ln.endswith("\n") else (ln + "\n") for ln in text.splitlines()]

    parts: List[Path] = []
    parts_meta: List[Dict[str, Any]] = []

    buf: List[str] = []
    buf_bytes = 0
    part_idx = 0

    def make_name(i: int) -> str:
        serial = f"{i+1:04d}"
        if group_dirs:
            # file-encoded group label so listing remains flat
            group = (i // max(1, parts_per_dir))
            g = f"{group:0{dir_suffix_width}d}"
            return f"{part_stem}_{g}_{serial}{part_ext}"
        return f"{part_stem}_{serial}{part_ext}"

    def flush():
        nonlocal buf, buf_bytes, part_idx
        if not buf:
            return
        name = make_name(part_idx)
        p = dest_dir / name
        p.write_text("".join(buf), encoding="utf-8")
        parts.append(p)
        parts_meta.append(
            {
                "name": p.name,
                "size": int(p.stat().st_size),
                "lines": len(buf),
            }
        )
        part_idx += 1
        buf = []
        buf_bytes = 0

    for s in lines:
        s_len = len(s.encode("utf-8"))
        if buf and (buf_bytes + s_len) > split_bytes:
            flush()
        buf.append(s)
        buf_bytes += s_len
    flush()

    index = {
        "record_type": "parts_index",
        "total_parts": len(parts_meta),
        "split_bytes": int(split_bytes),
        "parts": parts_meta,
        "source": src_manifest.name,
    }
    return parts, index


def _append_parts_artifacts_into_manifest(
    *,
    manifest_path: Path,
    parts_dir: Path,
    part_stem: str,
    part_ext: str,
    parts_index_name: str,
) -> int:
    """
    After parts are created, append their artifact records to the manifest so
    downstream tooling can discover them without scanning the filesystem.
    """
    app = ManifestAppender(manifest_path)
    count = emit_transport_parts(
        appender=app,
        parts_dir=parts_dir,
        part_stem=part_stem,
        part_ext=part_ext,
        parts_index_name=parts_index_name,
    )
    return count


def _maybe_chunk_manifest_and_update(
    *,
    cfg: NS,
    which: str,  # "local" | "github"
) -> Dict[str, Any]:
    """
    Decide whether to chunk cfg.out_bundle based on code_bundle params.
    If chunking happens:
      - Write part files under the artifact directory
      - Write parts index JSON
      - Append artifact records into the manifest
      - Remove monolith if cfg.transport.preserve_monolith is False
      - Update (or remove) SHA256SUMS accordingly
    Returns a small report dict.
    """
    params = _read_code_bundle_params()
    mode = params.get("chunk_manifest", "auto")
    split_bytes = int(params.get("split_bytes", 300000) or 300000)
    group_dirs = bool(params.get("group_dirs", True))

    manifest_path = Path(cfg.out_bundle)
    parts_dir = manifest_path.parent
    part_stem = str(cfg.transport.part_stem)
    part_ext = str(cfg.transport.part_ext)
    index_name = str(cfg.transport.parts_index_name)
    index_path = parts_dir / index_name

    report = {
        "kind": which,
        "decision": "skipped",
        "parts": 0,
        "bytes": int(manifest_path.stat().st_size) if manifest_path.exists() else 0,
        "split_bytes": split_bytes,
    }

    if not manifest_path.exists():
        print(f"[packager] chunk({which}): manifest missing; nothing to do")
        return report

    size = int(manifest_path.stat().st_size)
    if not _should_chunk(mode, size, split_bytes):
        # ensure sums (for monolith) if present
        write_sha256sums_for_file(target_file=manifest_path, out_sums_path=Path(cfg.out_sums))
        report["decision"] = "no-chunk"
        return report

    # Create parts + index (flat files; grouping encoded in filenames)
    parts, index = _write_parts_from_jsonl(
        src_manifest=manifest_path,
        dest_dir=parts_dir,
        part_stem=part_stem,
        part_ext=part_ext,
        split_bytes=split_bytes,
        group_dirs=group_dirs,
        dir_suffix_width=int(getattr(cfg.transport, "dir_suffix_width", 2)),
        parts_per_dir=int(getattr(cfg.transport, "parts_per_dir", 10)),
    )
    (parts_dir / index_name).write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    # Append artifact records for the newly-created parts
    added = _append_parts_artifacts_into_manifest(
        manifest_path=manifest_path,
        parts_dir=parts_dir,
        part_stem=part_stem,
        part_ext=part_ext,
        parts_index_name=index_name,
    )
    print(f"[packager] chunk({which}): wrote {len(parts)} parts; appended {added} artifact records")

    # Remove/keep monolith as per transport.preserve_monolith
    if not bool(getattr(cfg.transport, "preserve_monolith", False)):
        try:
            manifest_path.unlink(missing_ok=True)  # py3.8+: ignore error if not exists
        except TypeError:
            # Python < 3.8 compatibility
            if manifest_path.exists():
                manifest_path.unlink()
        # ensure no dangling sums
        write_sha256sums_for_file(target_file=manifest_path, out_sums_path=Path(cfg.out_sums))
    else:
        # refresh sums for monolith
        write_sha256sums_for_file(target_file=manifest_path, out_sums_path=Path(cfg.out_sums))

    report.update({"decision": "chunked", "parts": len(parts)})
    return report


# ──────────────────────────────────────────────────────────────────────────────
# GitHub publishing (forced artifacts under 'design_manifest/' at repo root)
# ──────────────────────────────────────────────────────────────────────────────
def _publish_to_github(
    cfg: NS,
    code_items_repo_rel: List[Tuple[Path, str]],
    *,
    manifest_override: Optional[Path] = None,
    sums_override: Optional[Path] = None,
) -> None:
    """
    Push codebase + artifacts to GitHub.
    - Code is published to the repo root using repo-relative paths.
    - Artifacts are published under 'design_manifest/' at the repo root.
    """
    if not cfg.publish.github:
        raise ConfigError("GitHub mode requires 'publish.github' coordinates")
    gh = cfg.publish.github
    token = str(cfg.publish.github_token or "").strip()
    if not token:
        raise ConfigError("GitHub mode requires a token")

    target = GitHubTarget(owner=gh.owner, repo=gh.repo, branch=gh.branch, base_path="")
    pub = GitHubPublisher(target=target, token=token)

    # Optionally clean remote artifacts directory first (safer than nuking repo root)
    if bool(getattr(cfg.publish, "clean_before_publish", False)):
        try:
            github_clean_remote_repo(
                owner=gh.owner, repo=gh.repo, branch=gh.branch, base_path="design_manifest", token=token
            )
        except Exception as e:
            print(f"[packager] WARN: remote clean failed: {type(e).__name__}: {e}")

    # 1) Publish code files at repo root
    print(f"[packager] Publish(GitHub): code files: {len(code_items_repo_rel)}")
    pub.publish_many_files(code_items_repo_rel, message="publish: code snapshot", throttle_every=50, sleep_secs=0.5)

    # 2) Publish artifacts under 'design_manifest/'
    art_dir = Path(cfg.out_bundle).parent
    candidates: List[Tuple[Path, str]] = []

    # Choose which manifest/sums to publish (override allows github-variant)
    manifest_path = Path(manifest_override) if manifest_override else Path(cfg.out_bundle)
    sums_path = Path(sums_override) if sums_override else Path(cfg.out_sums)

    # Standard artifacts
    for name in ("assistant_handoff.v1.json", "superbundle.run.json", "design_manifest.SHA256SUMS"):
        p = art_dir / name
        if name == "design_manifest.SHA256SUMS":
            p = sums_path  # explicit override target
        if p.exists() and p.is_file():
            candidates.append((p, f"design_manifest/{p.name}"))

    # Manifest (if monolith exists)
    if manifest_path.exists():
        candidates.append((manifest_path, f"design_manifest/{manifest_path.name}"))

    # Parts index and parts (if present)
    idx = art_dir / str(getattr(cfg.transport, "parts_index_name", "design_manifest_parts_index.json"))
    if idx.exists():
        candidates.append((idx, f"design_manifest/{idx.name}"))
    part_stem = str(getattr(cfg.transport, "part_stem", "design_manifest"))
    part_ext = str(getattr(cfg.transport, "part_ext", ".txt"))
    for p in sorted(art_dir.glob(f"{part_stem}*{part_ext}")):
        if p.is_file():
            candidates.append((p, f"design_manifest/{p.name}"))

    if not candidates:
        print("[packager] Publish(GitHub): nothing to publish in artifacts")
        return

    print(f"[packager] Publish(GitHub): artifacts: {len(candidates)}")
    pub.publish_many_files(candidates, message="publish: design manifest", throttle_every=50, sleep_secs=0.5)


# ──────────────────────────────────────────────────────────────────────────────
# Manifest enrichment helpers
# ──────────────────────────────────────────────────────────────────────────────
def _tool_versions() -> Dict[str, Any]:
    try:
        orch_path = Path(inspect.getsourcefile(orch_mod) or "")
        return {
            "packager.orchestrator": orch_path.as_posix() if orch_path else "?",
            "run_pack": Path(__file__).as_posix(),
        }
    except Exception:
        return {"run_pack": Path(__file__).as_posix()}


def _map_record_paths_inplace(rec: Dict[str, Any], map_path_fn) -> None:
    """
    Map path-like fields in-place. Best-effort for known shapes.
    This covers:
      - 'path'
      - 'src_path', 'dst_path'
      - lists of strings under rec['examples'][*] (best effort)
    """
    for key in ("path", "src_path", "dst_path"):
        if key in rec and isinstance(rec[key], str):
            rec[key] = map_path_fn(rec[key])

    # Best-effort: map examples.{license_files|headers|notices} lists
    examples = rec.get("examples")
    if isinstance(examples, dict):
        for k, v in list(examples.items()):
            if isinstance(v, list):
                examples[k] = [map_path_fn(x) if isinstance(x, str) else x for x in v]


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
      - NEW: doc.coverage, code.complexity, owners.index, env.index, entrypoints.index,
             html.index, sql.index, js_ts.index, deps.index (+ summary),
             git.* (repo/ignore/submodule/summary), license.* (file/header/notice/summary),
             secrets.* (finding/summary), asset.* (file/summary)

    path_mode:
      - "local"  : new records get paths prefixed with emitted_prefix
      - "github" : new records use repo-root relative paths
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
                # Respect both src_path / dst_path if present
                if "src_path" in e and isinstance(e["src_path"], str):
                    e["src_path"] = map_path(e["src_path"])
                else:
                    e["src_path"] = map_path(rel)
                if "dst_path" in e and isinstance(e["dst_path"], str):
                    e["dst_path"] = map_path(e["dst_path"])
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

    # --- NEW WIRED SCANNERS -------------------------------------------------
    # Helpers to invoke a scanner, map its 'path'-like fields, and append
    def run_scanner(name: str, fn, *args, **kwargs):
        try:
            records = fn(*args, **kwargs) or []
        except Exception as e:
            print(f"[packager] WARN: scanner '{name}' failed: {type(e).__name__}: {e}")
            return 0
        n = 0
        for rec in records:
            if isinstance(rec, dict):
                _map_record_paths_inplace(rec, map_path)
                app.append_record(rec)
                n += 1
        return n

    wired_counts: Dict[str, int] = {}
    wired_counts["doc_coverage"] = run_scanner("doc_coverage", scan_doc_coverage, Path(cfg.source_root), discovered_repo)
    wired_counts["complexity"] = run_scanner("complexity", scan_complexity, Path(cfg.source_root), discovered_repo)
    wired_counts["owners"] = run_scanner("owners_index", scan_owners, Path(cfg.source_root), discovered_repo)
    wired_counts["env"] = run_scanner("env_index", scan_env, Path(cfg.source_root), discovered_repo)
    wired_counts["entrypoints"] = run_scanner("entrypoints", scan_entrypoints, Path(cfg.source_root), discovered_repo)
    wired_counts["html"] = run_scanner("html_index", scan_html, Path(cfg.source_root), discovered_repo)
    wired_counts["sql"] = run_scanner("sql_index", scan_sql, Path(cfg.source_root), discovered_repo)
    wired_counts["js_ts"] = run_scanner("js_ts_index", scan_js_ts, Path(cfg.source_root), discovered_repo)
    wired_counts["deps"] = run_scanner("deps_index", scan_deps, Path(cfg.source_root), discovered_repo)
    wired_counts["git"] = run_scanner("git_info", scan_git, Path(cfg.source_root), discovered_repo)
    wired_counts["license"] = run_scanner("license_scan", scan_license, Path(cfg.source_root), discovered_repo)
    wired_counts["secrets"] = run_scanner("secrets_scan", scan_secrets, Path(cfg.source_root), discovered_repo)
    wired_counts["assets"] = run_scanner("assets_index", scan_assets, Path(cfg.source_root), discovered_repo)
    # ------------------------------------------------------------------------

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
            # include wired scanner record totals (approximate)
            **{f"wired.{k}": v for k, v in wired_counts.items()},
        },
        durations_ms={
            "index_ms": int((t1 - t0) * 1000),
            "quality_ms": int((t2 - t1) * 1000),
            "graph_ms": int((t3 - t2) * 1000),
        },
    )
    app.append_record(summary)

    print(
        "[packager] Augment manifest: "
        f"modules={module_count}, metrics={quality_count}, edges={len(edges_dedup)}, "
        f"artifacts={art_count}, path_mode={path_mode}, "
        "wired={" + ", ".join(f"{k}:{v}" for k, v in wired_counts.items()) + "}"
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
        clean_before_publish=True,  # we force clean of artifacts dir before publish in code below
    )

    # Provenance + active filters
    print(f"[packager] using orchestrator from: {inspect.getsourcefile(orch_mod) or '?'}")
    print(f"[packager] source_root: {cfg.source_root}")
    print(f"[packager] emitted_prefix: {cfg.emitted_prefix}")
    print(f"[packager] include_globs: {list(cfg.include_globs)}")
    print(f"[packager] exclude_globs: {list(cfg.exclude_globs)}")
    print(f"[packager] segment_excludes: {list(cfg.segment_excludes)}")
    print(f"[packager] follow_symlinks: {cfg.follow_symlinks} case_insensitive: {cfg.case_insensitive}")

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
    #  - local : manifest paths = local snapshot (prefix emitted_prefix)
    #  - github: manifest paths = repo-root
    #  - both  : local manifest stays local; github gets a rewritten copy
    manifest_path = Path(cfg.out_bundle)
    sums_path = Path(cfg.out_sums)
    emitted_prefix = str(cfg.emitted_prefix).strip("/")

    # Helper: rewrite manifest to a target path with specific path mode
    def _rewrite_to_mode(manifest_in: Path, out_path: Path, to_mode: str):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rewrite_manifest_paths(
            manifest_in=manifest_in,
            manifest_out=out_path,
            emitted_prefix=emitted_prefix,
            to_mode=to_mode,
        )
        return out_path

    # 1) LOCAL enrich (if local or both)
    if do_local:
        # ensure any file paths remain local-mode for augmentation
        # (no-op rewrite; keep same target file)
        augment_manifest(
            cfg=cfg,
            discovered_repo=discovered_repo,
            mode_local=do_local,
            mode_github=do_github,
            path_mode="local",
        )
        # Defer chunking until after augmentation (so parts include augmented lines)

    # 2) GITHUB enrich (github-only or both)
    gh_manifest_override: Optional[Path] = None
    gh_sums_override: Optional[Path] = None
    if do_github:
        # Create a GitHub-paths variant of the manifest next to local one
        gh_manifest_override = manifest_path.parent / "design_manifest.github.jsonl"
        _rewrite_to_mode(manifest_in=manifest_path, out_path=gh_manifest_override, to_mode="github")
        # Temporarily point cfg.out_bundle/sums to GH variant for augmentation
        local_bundle, local_sums = cfg.out_bundle, cfg.out_sums
        try:
            cfg.out_bundle = gh_manifest_override
            cfg.out_sums = manifest_path.parent / "design_manifest.github.SHA256SUMS"
            augment_manifest(
                cfg=cfg,
                discovered_repo=discovered_repo,
                mode_local=do_local,
                mode_github=do_github,
                path_mode="github",
            )
            gh_sums_override = Path(cfg.out_sums)
        finally:
            cfg.out_bundle = local_bundle
            cfg.out_sums = local_sums

    # ── CHUNKING (transport parts) ─────────────────────────────────────────
    # Perform after augmentation so parts include *all* manifest lines.
    # LOCAL
    if do_local:
        rep = _maybe_chunk_manifest_and_update(cfg=cfg, which="local")
        print(f"[packager] chunk report (local): {rep}")

    # GITHUB variant (if produced)
    if do_github and gh_manifest_override and gh_manifest_override.exists():
        # Use a temporary cfg with override bundle/sums targeting GH variant
        local_bundle, local_sums = cfg.out_bundle, cfg.out_sums
        try:
            cfg.out_bundle = gh_manifest_override
            cfg.out_sums = gh_sums_override or (gh_manifest_override.parent / "design_manifest.github.SHA256SUMS")
            rep = _maybe_chunk_manifest_and_update(cfg=cfg, which="github")
            print(f"[packager] chunk report (github): {rep}")
        finally:
            cfg.out_bundle = local_bundle
            cfg.out_sums = local_sums

    # ── SHA256SUMS (finalize for local monolith if it still exists) ───────
    if do_local and Path(cfg.out_bundle).exists():
        write_sha256sums_for_file(target_file=Path(cfg.out_bundle), out_sums_path=Path(cfg.out_sums))

    # ── PUBLISH TO GITHUB (code + artifacts) ───────────────────────────────
    if do_github:
        _publish_to_github(
            cfg=cfg,
            code_items_repo_rel=discovered_repo,
            manifest_override=gh_manifest_override if (gh_manifest_override and gh_manifest_override.exists()) else None,
            sums_override=gh_sums_override if (gh_sums_override and gh_sums_override.exists()) else None,
        )

    print("[packager] done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as ce:
        print(f"[packager] CONFIG ERROR: {ce}")
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("[packager] interrupted.")
        raise SystemExit(130)
