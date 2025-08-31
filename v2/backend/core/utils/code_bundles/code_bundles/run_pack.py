"""
Packager runner (direct-source). Single source of truth:
- Config:   config/packager.yml  (root-level 'publish_analysis' + publish.* + emit_ast)
- Token:    secret_management/secrets.yml -> github.api_key
- No reads of publish.local.json.

Code publish respects publish.github.base_path.
Artifacts publish to repo-root/design_manifest/ (including analysis/** when enabled).
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
from typing import Any, Dict, List, Optional, Tuple, Iterable
from urllib import error, parse, request

# Ensure the embedded packager is importable first
ROOT = Path(__file__).parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Embedded packager
from packager.core.orchestrator import Packager
import packager.core.orchestrator as orch_mod  # provenance
from packager.io.publisher import GitHubPublisher, GitHubTarget

# Manifest helpers + enrichment
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

# Wired scanners
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

# YAML reader to fetch root-level flags from packager.yml
import yaml


class Transport(NS):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Discovery helpers
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


def _seg_excluded(parts: Tuple[str, ...], segment_excludes: List[str], case_insensitive: bool = False) -> bool:
    if not segment_excludes:
        return False
    segs = set((s.casefold() if case_insensitive else s) for s in segment_excludes)
    for seg in parts[:-1]:
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
    out: List[Tuple[Path, str]] = []
    for cur, dirs, files in os.walk(src_root, followlinks=follow_symlinks):
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
            if include_globs and not _match_any(rel_posix, include_globs, case_insensitive):
                continue
            if exclude_globs and _match_any(rel_posix, exclude_globs, case_insensitive):
                continue
            out.append((p, rel_posix))
    out.sort(key=lambda t: t[1])
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Local snapshot utilities
# ──────────────────────────────────────────────────────────────────────────────
def _clear_dir_contents(root: Path) -> None:
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
        resp.read()


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
    stack = [path]
    seen = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
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
    root = (base_path or "").strip("/")
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


def _print_full_raw_links(owner: str, repo: str, branch: str, token: str) -> None:
    print("\n=== Raw GitHub Links (full repo) ===")
    all_files = list(_gh_walk_files(owner, repo, "", branch, token))
    for it in sorted(all_files, key=lambda d: d["path"]):
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{it['path']}"
        print(url)
    print(f"=== ({len(all_files)} files) ===\n")


# ──────────────────────────────────────────────────────────────────────────────
# Config readers (+ root-level flags)
# ──────────────────────────────────────────────────────────────────────────────
def _read_root_publish_analysis() -> bool:
    """
    Read config/packager.yml directly to respect ROOT-LEVEL 'publish_analysis'.
    Do NOT infer from publish.*. Only return the root-level boolean.
    """
    try:
        paths = ConfigPaths.detect()
        cfg_path = paths.repo_root / "config" / "packager.yml"
        if not cfg_path.exists():
            return False
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return bool(data.get("publish_analysis", False))
    except Exception as e:
        print(f"[packager] WARN: publish_analysis read failed: {type(e).__name__}: {e}")
        return False


def _read_root_emit_ast() -> bool:
    """
    Read config/packager.yml directly to respect ROOT-LEVEL 'emit_ast'.
    This only controls whether we append AST records if produced by the indexer.
    """
    try:
        paths = ConfigPaths.detect()
        cfg_path = paths.repo_root / "config" / "packager.yml"
        if not cfg_path.exists():
            return False
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return bool(data.get("emit_ast", False))
    except Exception as e:
        print(f"[packager] WARN: emit_ast read failed: {type(e).__name__}: {e}")
        return False


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
    publish_analysis: bool = False,   # <- root-level flag passed in
    publish_handoff: bool = True,
    publish_transport: bool = True,
    local_publish_root: Optional[Path] = None,
    clean_before_publish: bool = False,
    emit_ast: bool = False,           # <- root-level flag passed in
) -> NS:
    pack = get_packager()
    repo_root = get_repo_root()

    out_bundle = (artifact_out / "design_manifest.jsonl").resolve()
    out_runspec = (artifact_out / "superbundle.run.json").resolve()
    out_guide = (artifact_out / "assistant_handoff.v1.json").resolve()
    out_sums = (artifact_out / "design_manifest.SHA256SUMS").resolve()

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

    case_insensitive = True if os.name == "nt" else False
    follow_symlinks = True

    publish = NS(
        mode=mode,
        publish_codebase=bool(publish_codebase),
        publish_analysis=bool(publish_analysis),  # <- strictly the root-level flag we read
        publish_handoff=bool(publish_handoff),
        publish_transport=bool(publish_transport),
        github=gh,
        github_token=(gh_token or ""),
        local_publish_root=(local_publish_root.resolve() if local_publish_root else None),
        clean_before_publish=bool(clean_before_publish),
    )

    cfg = NS(
        source_root=src,
        emitted_prefix=getattr(pack, "emitted_prefix", "output/patch_code_bundles"),
        include_globs=list(getattr(pack, "include_globs", ["**/*"])),
        exclude_globs=list(getattr(pack, "exclude_globs", [])),
        follow_symlinks=follow_symlinks,
        case_insensitive=case_insensitive,
        segment_excludes=list(getattr(pack, "segment_excludes", [])),
        out_bundle=out_bundle,
        out_runspec=out_runspec,
        out_guide=out_guide,
        out_sums=out_sums,
        transport=transport,
        publish=publish,
        prompts=None,
        prompt_mode="none",
        emit_ast=bool(emit_ast),
    )

    artifact_out.mkdir(parents=True, exist_ok=True)
    (repo_root / "").exists()
    return cfg


# ──────────────────────────────────────────────────────────────────────────────
# Code-bundle params from vars.yml
# ──────────────────────────────────────────────────────────────────────────────
def _read_code_bundle_params() -> Dict[str, Any]:
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
            }
            if out["chunk_manifest"] not in {"auto", "always", "never"}:
                out["chunk_manifest"] = "auto"
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
        parts_meta.append({"name": p.name, "size": int(p.stat().st_size), "lines": len(buf)})
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
    params = _read_code_bundle_params()
    mode = params.get("chunk_manifest", "auto")
    split_bytes = int(params.get("split_bytes", 300000) or 300000)
    group_dirs = bool(params.get("group_dirs", True))

    manifest_path = Path(cfg.out_bundle)
    parts_dir = manifest_path.parent
    part_stem = str(cfg.transport.part_stem)
    part_ext = str(cfg.transport.part_ext)
    index_name = str(cfg.transport.parts_index_name)

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
        write_sha256sums_for_file(target_file=manifest_path, out_sums_path=Path(cfg.out_sums))
        report["decision"] = "no-chunk"
        return report

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

    added = _append_parts_artifacts_into_manifest(
        manifest_path=manifest_path,
        parts_dir=parts_dir,
        part_stem=part_stem,
        part_ext=part_ext,
        parts_index_name=index_name,
    )
    print(f"[packager] chunk({which}): wrote {len(parts)} parts; appended {added} artifact records")

    if not bool(getattr(cfg.transport, "preserve_monolith", False)):
        try:
            manifest_path.unlink(missing_ok=True)
        except TypeError:
            if manifest_path.exists():
                manifest_path.unlink()
        write_sha256sums_for_file(target_file=manifest_path, out_sums_path=Path(cfg.out_sums))
    else:
        write_sha256sums_for_file(target_file=manifest_path, out_sums_path=Path(cfg.out_sums))

    report.update({"decision": "chunked", "parts": len(parts)})
    return report


# ──────────────────────────────────────────────────────────────────────────────
# GitHub publishing (now includes analysis/**)
# ──────────────────────────────────────────────────────────────────────────────
def _publish_to_github(
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
                owner=gh.owner, repo=gh.repo, branch=gh.branch, base_path="design_manifest", token=token
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


# ──────────────────────────────────────────────────────────────────────────────
# Delta pruning (code + artifacts)
# ──────────────────────────────────────────────────────────────────────────────
def _is_managed_path(
    rel_posix: str,
    include_globs: List[str],
    exclude_globs: List[str],
    segment_excludes: List[str],
    case_insensitive: bool,
) -> bool:
    parts = Path(rel_posix).parts
    if _seg_excluded(parts, segment_excludes, case_insensitive):
        return False
    if include_globs and not _match_any(rel_posix, include_globs, case_insensitive):
        return False
    if exclude_globs and _match_any(rel_posix, exclude_globs, case_insensitive):
        return False
    return True


def _prune_remote_code_delta(
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
    remote_files = list(_gh_walk_files(gh_owner, gh_repo, base_prefix, gh_branch, token))
    to_delete = []
    for it in remote_files:
        path = it["path"]
        # Never touch design_manifest subtree here
        if path.startswith("design_manifest/"):
            continue
        rel_for_rules = path[len(base_prefix) + 1 :] if base_prefix and path.startswith(base_prefix + "/") else path
        if not _is_managed_path(rel_for_rules, include_globs, exclude_globs, seg_excludes, casei):
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
            _gh_delete_file(gh_owner, gh_repo, it["path"], it["sha"], gh_branch, token, "remove stale file (code)")
            deleted += 1
            if i and (i % 50 == 0):
                time.sleep(0.5)
        except Exception as e:
            print(f"[packager] WARN: failed delete (code) {it['path']}: {type(e).__name__}: {e}")
    return deleted


def _prune_remote_artifacts_delta(
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
    remote = list(_gh_walk_files(gh_owner, gh_repo, "design_manifest", gh_branch, token))
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
            _gh_delete_file(gh_owner, gh_repo, it["path"], it["sha"], gh_branch, token, "remove stale file (artifacts)")
            deleted += 1
            if i and (i % 50 == 0):
                time.sleep(0.5)
        except Exception as e:
            print(f"[packager] WARN: failed delete (artifacts) {it['path']}: {type(e).__name__}: {e}")
    return deleted


# ──────────────────────────────────────────────────────────────────────────────
# Manifest enrichment
# ──────────────────────────────────────────────────────────────────────────────
def _tool_versions() -> Dict[str, Any]:
    try:
        orch_path = Path(inspect.getsourcefile(orch_mod) or "")
        return {"packager.orchestrator": orch_path.as_posix() if orch_path else "?", "run_pack": Path(__file__).as_posix()}
    except Exception:
        return {"run_pack": Path(__file__).as_posix()}


def _map_record_paths_inplace(rec: Dict[str, Any], map_path_fn) -> None:
    # Standard path keys
    for key in ("path", "src_path", "dst_path", "caller_path", "callee_path"):
        if key in rec and isinstance(rec[key], str):
            rec[key] = map_path_fn(rec[key])
    # Examples array-of-paths
    examples = rec.get("examples")
    if isinstance(examples, dict):
        for k, v in list(examples.items()):
            if isinstance(v, list):
                examples[k] = [map_path_fn(x) if isinstance(x, str) else x for x in v]


def _append_records(app: ManifestAppender, records: Optional[Iterable[Dict[str, Any]]], map_path_fn) -> int:
    n = 0
    if not records:
        return 0
    for rec in records:
        if isinstance(rec, dict):
            _map_record_paths_inplace(rec, map_path_fn)
            app.append_record(rec)
            n += 1
    return n


def augment_manifest(
    *,
    cfg: NS,
    discovered_repo: List[Tuple[Path, str]],
    mode_local: bool,
    mode_github: bool,
    path_mode: str,
) -> None:
    app = ManifestAppender(Path(cfg.out_bundle))

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

    t0 = t1 = t2 = t3 = time.perf_counter()
    module_count = 0
    quality_count = 0
    edges_accum: List[Dict[str, Any]] = []

    ast_symbols = 0
    ast_xrefs = 0
    ast_calls = 0
    ast_docstrings = 0
    ast_symmetrics = 0

    # Python indexing: modules + edges (+ optional AST if provided by indexer)
    for local, rel in discovered_repo:
        if not rel.endswith(".py"):
            continue

        # Optional emit_ast support (backwards compatible with existing signature)
        use_emit_ast = bool(getattr(cfg, "emit_ast", False))
        res = None
        try:
            sig = inspect.signature(index_python_file)
            if "emit_ast" in sig.parameters:
                res = index_python_file(
                    repo_root=Path(cfg.source_root),
                    local_path=local,
                    repo_rel_posix=rel,
                    emit_ast=use_emit_ast,
                )
            else:
                res = index_python_file(
                    repo_root=Path(cfg.source_root),
                    local_path=local,
                    repo_rel_posix=rel,
                )
        except Exception as e:
            # Fallback to legacy call if signature probing failed oddly
            try:
                res = index_python_file(
                    repo_root=Path(cfg.source_root),
                    local_path=local,
                    repo_rel_posix=rel,
                )
            except Exception as e2:
                print(f"[packager] WARN: python_index failed for {rel}: {type(e2).__name__}: {e2}")
                continue

        mod_rec = None
        edges: List[Dict[str, Any]] = []
        extras: Optional[Any] = None

        if isinstance(res, (list, tuple)):
            if len(res) >= 1:
                mod_rec = res[0]
            if len(res) >= 2:
                edges = list(res[1] or [])
            if len(res) >= 3:
                extras = res[2]
        elif isinstance(res, dict):
            # If indexer returns a dict-like
            mod_rec = res.get("module")
            edges = list(res.get("edges") or [])
            extras = res.get("ast")

        # module
        if mod_rec:
            mod_rec["path"] = map_path(rel)
            app.append_record(mod_rec)
            module_count += 1

        # edges
        if edges:
            for e in edges:
                e["src_path"] = map_path(e.get("src_path") or rel)
                if "dst_path" in e and isinstance(e["dst_path"], str):
                    e["dst_path"] = map_path(e["dst_path"])
            edges_accum.extend(edges)

        # optional AST extras (only when cfg.emit_ast is True and indexer returned them)
        if extras and bool(getattr(cfg, "emit_ast", False)):
            # Accept either dict/NS with named lists or flat list of records
            def _take_list(name: str) -> List[Dict[str, Any]]:
                if isinstance(extras, dict):
                    v = extras.get(name)
                else:
                    v = getattr(extras, name, None)
                return list(v or [])

            # If extras is a flat list of records, treat them generically
            if isinstance(extras, (list, tuple)) and all(isinstance(x, dict) for x in extras):
                ast_symbols += _append_records(app, extras, map_path)
            else:
                ast_symbols += _append_records(app, _take_list("symbols"), map_path)
                ast_xrefs += _append_records(app, _take_list("xrefs"), map_path)
                ast_calls += _append_records(app, _take_list("calls"), map_path)
                ast_docstrings += _append_records(app, _take_list("docstrings"), map_path)
                ast_symmetrics += _append_records(app, _take_list("symbol_metrics"), map_path)

    t1 = time.perf_counter()

    # Per-file quality metrics
    for local, rel in discovered_repo:
        if not rel.endswith(".py"):
            continue
        qrec = quality_for_python(path=local, repo_rel_posix=rel)
        qrec["path"] = map_path(rel)
        app.append_record(qrec)
        quality_count += 1

    t2 = time.perf_counter()

    # Coalesce import edges
    edges_dedup = coalesce_edges(edges_accum)
    for e in edges_dedup:
        app.append_record(e)

    t3 = time.perf_counter()

    # Run wired scanners
    def run_scanner(name: str, fn, *args, **kwargs):
        try:
            records = fn(*args, **kwargs) or []
        except Exception as e:
            print(f"[packager] WARN: scanner '{name}' failed: {type(e).__name__}: {e}")
            return 0
        return _append_records(app, records, map_path)

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

    # Standard artifacts + transport parts emission records
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

    # Build summary with AST counts if any
    counts_base = {
        "files": len(discovered_repo),
        "modules": module_count,
        "edges": len(edges_dedup),
        "metrics": quality_count,
        "artifacts": art_count,
        **{f"wired.{k}": v for k, v in wired_counts.items()},
    }
    if bool(getattr(cfg, "emit_ast", False)):
        counts_base.update(
            {
                "ast.symbols": ast_symbols,
                "ast.xrefs": ast_xrefs,
                "ast.calls": ast_calls,
                "ast.docstrings": ast_docstrings,
                "ast.symbol_metrics": ast_symmetrics,
            }
        )

    summary = build_bundle_summary(
        counts=counts_base,
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
        + ("ast={symbols:%d, xrefs:%d, calls:%d, docstrings:%d, symmetrics:%d}, "
           % (ast_symbols, ast_xrefs, ast_calls, ast_docstrings, ast_symmetrics)
           if bool(getattr(cfg, 'emit_ast', False)) else "")
        + "wired={" + ", ".join(f"{k}:{v}" for k, v in wired_counts.items()) + "}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
def main() -> int:
    pack = get_packager()
    repo_root = get_repo_root()

    pub = dict(getattr(pack, "publish", {}) or {})
    mode = str(pub.get("mode", "local")).lower()
    if mode not in {"local", "github", "both"}:
        raise ConfigError("publish.mode must be 'local', 'github', or 'both'")
    do_local = mode in {"local", "both"}
    do_github = mode in {"github", "both"}
    print(f"[packager] mode: {mode} (local={do_local}, github={do_github})")

    # honor ROOT-LEVEL flags only
    root_publish_analysis = _read_root_publish_analysis()
    root_emit_ast = _read_root_emit_ast()
    print(f"[packager] publish_analysis (root-level): {root_publish_analysis}")
    print(f"[packager] emit_ast (root-level): {root_emit_ast}")

    clean_repo_root = bool(pub.get("clean_repo_root", False))
    clean_artifacts = bool(pub.get("clean_artifacts", pub.get("clean_before_publish", False)))

    artifact_root = (repo_root / "output" / "design_manifest").resolve()
    code_output_root = (repo_root / "output" / "patch_code_bundles").resolve()
    source_root = repo_root

    github = dict(pub.get("github") or {})
    gh_owner = str(github.get("owner") or "").strip()
    gh_repo = str(github.get("repo") or "").strip()
    gh_branch = str(github.get("branch") or "main").strip()
    gh_base = str(github.get("base_path") or "").strip()

    secrets = get_secrets(ConfigPaths.detect())
    gh_token = str(secrets.github_token or "").strip()

    if do_github:
        if not gh_token:
            raise ConfigError("GitHub token not found. Set secret_management/secrets.yml -> github.api_key")
        missing = [k for k, v in (("owner", gh_owner), ("repo", gh_repo), ("branch", gh_branch)) if not v]
        if missing:
            raise ConfigError(
                f"Missing GitHub {'/'.join(missing)}. Set these in config/packager.yml under publish.github.{{owner,repo,branch}}."
            )

    cfg = build_cfg(
        src=source_root,
        artifact_out=artifact_root,
        publish_mode=mode,
        gh_owner=gh_owner if gh_owner else None,
        gh_repo=gh_repo if gh_repo else None,
        gh_branch=gh_branch or "main",
        gh_base=gh_base,
        gh_token=gh_token if do_github else None,
        publish_codebase=bool(pub.get("publish_codebase", True)),
        publish_analysis=root_publish_analysis,  # <- root-level only
        publish_handoff=bool(pub.get("publish_handoff", True)),
        publish_transport=bool(pub.get("publish_transport", True)),
        local_publish_root=None,
        clean_before_publish=bool(clean_artifacts) if (do_github and not clean_repo_root) else False,
        emit_ast=root_emit_ast,  # <- root-level only
    )

    print(f"[packager] using orchestrator from: {inspect.getsourcefile(orch_mod) or '?'}")
    print(f"[packager] source_root: {cfg.source_root}")
    print(f"[packager] emitted_prefix: {cfg.emitted_prefix}")
    print(f"[packager] include_globs: {list(cfg.include_globs)}")
    print(f"[packager] exclude_globs: {list(cfg.exclude_globs)}")
    print(f"[packager] segment_excludes: {list(cfg.segment_excludes)}")
    print(f"[packager] follow_symlinks: {cfg.follow_symlinks} case_insensitive: {cfg.case_insensitive}")
    print("[packager] Packager: start]")

    if do_local:
        _clear_dir_contents(artifact_root)
        _clear_dir_contents(code_output_root)

    result = Packager(cfg, rules=None).run(external_source=None)
    print(f"Bundle: {result.out_bundle}")
    print(f"Run-spec: {result.out_runspec}")
    print(f"Guide: {result.out_guide}")

    discovered_repo = discover_repo_paths(
        src_root=cfg.source_root,
        include_globs=list(cfg.include_globs),
        exclude_globs=list(cfg.exclude_globs),
        segment_excludes=list(cfg.segment_excludes),
        case_insensitive=bool(getattr(cfg, "case_insensitive", False)),
        follow_symlinks=bool(getattr(cfg, "follow_symlinks", False)),
    )
    print(f"[packager] discovered repo files: {len(discovered_repo)}")

    if do_local:
        copied = copy_snapshot(discovered_repo, code_output_root)
        print(f"[packager] Local snapshot: copied {copied} files to {code_output_root}")

    # LOCAL augment
    if do_local:
        augment_manifest(
            cfg=cfg,
            discovered_repo=discovered_repo,
            mode_local=do_local,
            mode_github=do_github,
            path_mode="local",
        )

    # GITHUB augment (github-path variant)
    manifest_path = Path(cfg.out_bundle)
    gh_manifest_override: Optional[Path] = None
    gh_sums_override: Optional[Path] = None
    emitted_prefix = str(cfg.emitted_prefix).strip("/")

    def _rewrite_to_mode(manifest_in: Path, out_path: Path, to_mode: str):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rewrite_manifest_paths(
            manifest_in=manifest_in,
            manifest_out=out_path,
            emitted_prefix=emitted_prefix,
            to_mode=to_mode,
        )
        return out_path

    if do_github:
        gh_manifest_override = manifest_path.parent / "design_manifest.github.jsonl"
        _rewrite_to_mode(manifest_in=manifest_path, out_path=gh_manifest_override, to_mode="github")
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
            cfg.out_bundle, cfg.out_sums = local_bundle, local_sums

    # Chunking (after augmentation)
    if do_local:
        rep = _maybe_chunk_manifest_and_update(cfg=cfg, which="local")
        print(f"[packager] chunk report (local): {rep}")

    if do_github and gh_manifest_override and gh_manifest_override.exists():
        local_bundle, local_sums = cfg.out_bundle, cfg.out_sums
        try:
            cfg.out_bundle = gh_manifest_override
            cfg.out_sums = gh_sums_override or (gh_manifest_override.parent / "design_manifest.github.SHA256SUMS")
            rep = _maybe_chunk_manifest_and_update(cfg=cfg, which="github")
            print(f"[packager] chunk report (github): {rep}")
        finally:
            cfg.out_bundle, cfg.out_sums = local_bundle, local_sums

    if do_local and Path(cfg.out_bundle).exists():
        write_sha256sums_for_file(target_file=Path(cfg.out_bundle), out_sums_path=Path(cfg.out_sums))

    # GitHub publish (includes analysis/** when root-level flag is true)
    if do_github:
        if clean_repo_root:
            try:
                github_clean_remote_repo(owner=gh_owner, repo=gh_repo, branch=gh_branch, base_path="", token=str(gh_token))
            except Exception as e:
                print(f"[packager] WARN: full repo clean failed: {type(e).__name__}: {e}")

            _publish_to_github(
                cfg=cfg,
                code_items_repo_rel=discovered_repo,
                base_path=gh_base,
                manifest_override=gh_manifest_override if (gh_manifest_override and gh_manifest_override.exists()) else None,
                sums_override=gh_sums_override if (gh_sums_override and gh_sums_override.exists()) else None,
            )
            _print_full_raw_links(gh_owner, gh_repo, gh_branch, str(gh_token))
        else:
            _publish_to_github(
                cfg=cfg,
                code_items_repo_rel=discovered_repo,
                base_path=gh_base,
                manifest_override=gh_manifest_override if (gh_manifest_override and gh_manifest_override.exists()) else None,
                sums_override=gh_sums_override if (gh_sums_override and gh_sums_override.exists()) else None,
            )
            try:
                deleted_code = _prune_remote_code_delta(
                    cfg=cfg,
                    gh_owner=gh_owner,
                    gh_repo=gh_repo,
                    gh_branch=gh_branch,
                    token=str(gh_token),
                    discovered_repo=discovered_repo,
                    base_path=gh_base,
                )
                deleted_art = _prune_remote_artifacts_delta(
                    cfg=cfg,
                    gh_owner=gh_owner,
                    gh_repo=gh_repo,
                    gh_branch=gh_branch,
                    token=str(gh_token),
                )
                print(f"[packager] Delta prune: code={deleted_code}, artifacts={deleted_art}")
            finally:
                _print_full_raw_links(gh_owner, gh_repo, gh_branch, str(gh_token))

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
