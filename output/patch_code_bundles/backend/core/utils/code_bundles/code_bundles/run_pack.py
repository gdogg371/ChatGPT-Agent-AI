#v2/backend/core/utils/code_bundles/code_bundles/run_pack.py
"""
YAML-driven packager runner (no env, no hardcoded defaults).

- Loads all configuration from the centralized loader's packager config and secrets.
- No CLI defaults; this script does not invent paths or tokens.
- Fails fast if required YAML keys are missing.
- Platform-agnostic path handling.

Expected YAML (config/packager.yml):
  emitted_prefix: "<string>"
  include_globs: [ ... ]
  exclude_globs: [ ... ]
  segment_excludes: [ ... ]
  publish:
    mode: "local" | "github" | "both"
    staging_root: "<path>"         # where the mirrored codebase will be maintained
    output_root: "<path>"          # where bundle/guide/runspec will be written
    ingest_root: "<path>"          # external source tree to copy into staging_root/codebase
    local_publish_root: "<path>"   # optional, used when mode=local or both
    clean_before_publish: true|false
    github:
      owner: "<owner>"
      repo: "<repo>"
      branch: "<branch>"
      base_path: "<subdir in repo>"

Secrets (secret_management/secrets.yml):
  github:
    token: "<PAT or fine-grained token>"
"""

from __future__ import annotations

import sys
import json
import fnmatch
import inspect
import time
from urllib import request, parse, error
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional

# Ensure we import the LOCAL packager from ./src (force it ahead of site-packages)
ROOT = Path(__file__).parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from packager.core.orchestrator import Packager  # always local now
import packager.core.orchestrator as orch_mod     # for provenance print

from v2.backend.core.configuration.loader import (
    get_repo_root,
    get_packager,
    get_secrets,
    ConfigError,
    ConfigPaths,
)


# --- Tiny holder classes to avoid config import mismatches ---------------------
class Transport(NS):
    pass


class GitHubTarget(NS):
    pass


class PublishOptions(NS):
    pass


def _bool(x, default=False) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        return x.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(x, (int, float)):
        return bool(x)
    return default


def _is_excluded(rel: str, exclude_globs: List[str], segment_excludes: List[str]) -> bool:
    """Mirror packager filters for the URL printer."""
    # glob-based excludes
    for pat in exclude_globs or []:
        if fnmatch.fnmatch(rel, pat):
            # Exception: allow the mirror subtree even if '**/output/**' matches
            if rel.startswith("output/patch_code_bundles/"):
                continue
            return True

    # segment-based excludes
    parts = set(Path(rel).parts)
    segs = set(segment_excludes or [])
    # Exception for the mirror subtree: ignore 'output' segment
    if rel.startswith("output/patch_code_bundles/"):
        segs.discard("output")
    if any(seg in parts for seg in segs):
        return True
    return False


def gather_emitted_paths(
    src_root: Path,
    emitted_prefix: str,
    *,
    exclude_globs: List[str] | None = None,
    segment_excludes: List[str] | None = None,
) -> List[str]:
    """
    Return files under src_root, filtered like the packager, prefixed by emitted_prefix.
    Normalizes away a leading 'codebase/' if present in rel paths.
    """
    prefix = emitted_prefix if emitted_prefix.endswith("/") else (emitted_prefix + "/")
    out: List[str] = []
    for p in sorted(src_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src_root).as_posix()
        if rel.startswith("codebase/"):
            rel = rel[len("codebase/") :]
        if _is_excluded(rel, exclude_globs or [], segment_excludes or []):
            continue
        out.append(f"{prefix}{rel}")
    return out


def print_github_raw_urls(owner: str, repo: str, branch: str, base_path: str, paths: List[str]) -> None:
    """
    Print raw.githubusercontent.com URLs for each emitted path.
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
    """
    Recursively delete ALL files under base_path ('' means repo root) on GitHub.
    """
    root = base_path.strip("/")
    print(
        f"[packager] Publish(GitHub): cleaning remote repo (owner={owner} repo={repo} branch={branch} base='{root or '/'}')"
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


# --------------------------- Runner (YAML only) --------------------------------

def _require_publish_key(pub: Dict[str, Any], key: str) -> Any:
    if key not in pub or pub[key] in (None, ""):
        raise ConfigError(f"packager.yml:publish.{key} is required")
    return pub[key]


def main() -> int:
    # Load config + secrets (NO fallbacks)
    pack = get_packager()
    secrets = get_secrets(ConfigPaths.detect())
    repo_root = get_repo_root()

    pub = dict(pack.publish or {})
    mode = str(_require_publish_key(pub, "mode")).lower()
    if mode not in {"local", "github", "both"}:
        raise ConfigError("packager.yml:publish.mode must be 'local', 'github', or 'both'")

    # Required locations (relative paths resolved from repo root)
    staging_root = Path(_require_publish_key(pub, "staging_root"))
    if not staging_root.is_absolute():
        staging_root = (repo_root / staging_root).resolve()

    output_root = Path(_require_publish_key(pub, "output_root"))
    if not output_root.is_absolute():
        output_root = (repo_root / output_root).resolve()

    ingest_root = Path(_require_publish_key(pub, "ingest_root"))
    if not ingest_root.is_absolute():
        ingest_root = (repo_root / ingest_root).resolve()

    # Optional publish extras
    clean_before_publish = bool(pub.get("clean_before_publish", False))
    local_publish_root = pub.get("local_publish_root")
    if local_publish_root:
        lpr = Path(str(local_publish_root))
        if not lpr.is_absolute():
            lpr = (repo_root / lpr).resolve()
        local_publish_root = lpr
    github = dict(pub.get("github") or {})
    gh_owner = github.get("owner")
    gh_repo = github.get("repo")
    gh_branch = github.get("branch", "main")
    gh_base = github.get("base_path", "")

    gh_token = secrets.github_token

    # Ensure roots exist
    staging_root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)

    # If the staging root itself contains a "codebase" folder, treat THAT as the real src.
    src_dir = staging_root
    if (src_dir / "codebase").exists():
        src_dir = src_dir / "codebase"

    # Build Packager config (SimpleNamespace, as orchestrator is attr-based)
    out_bundle = output_root / "design_manifest.jsonl"
    out_runspec = output_root / "superbundle.run.json"
    out_guide = output_root / "assistant_handoff.v1.json"
    out_sums = output_root / "design_manifest.SHA256SUMS"

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
    if mode in ("github", "both"):
        gh = GitHubTarget(owner=gh_owner or "", repo=gh_repo or "", branch=gh_branch or "main", base_path=gh_base or "")

    publish = PublishOptions(
        mode=mode,
        publish_codebase=bool(pub.get("publish_codebase", True)),
        publish_analysis=bool(pub.get("publish_analysis", False)),
        publish_handoff=bool(pub.get("publish_handoff", True)),
        publish_transport=bool(pub.get("publish_transport", True)),
        github=gh,
        github_token=(gh_token or ""),
        local_publish_root=local_publish_root,
        clean_before_publish=bool(clean_before_publish),
    )

    cfg = NS(
        # staging / discovery
        source_root=src_dir,
        emitted_prefix=pack.emitted_prefix,
        include_globs=list(pack.include_globs),
        exclude_globs=list(pack.exclude_globs),
        follow_symlinks=False,
        case_insensitive=False,
        segment_excludes=list(pack.segment_excludes),
        # outputs
        out_bundle=out_bundle,
        out_runspec=out_runspec,
        out_guide=out_guide,
        out_sums=out_sums,
        # features
        transport=transport,
        publish=publish,
        # prompts (unused; keep explicit)
        prompts=None,
        prompt_mode="none",
    )

    # --- Provenance + active filters (so you can SEE what's actually used) -----
    print(f"[packager] using orchestrator from: {inspect.getsourcefile(orch_mod) or '?'}")
    print(f"[packager] src_dir: {src_dir}")
    print(f"[packager] emitted_prefix: {cfg.emitted_prefix}")
    print(f"[packager] exclude_globs: {list(cfg.exclude_globs)}")
    print(f"[packager] segment_excludes: {list(cfg.segment_excludes)}")
    print("[packager] Packager: start")

    if not ingest_root.exists():
        print(f"[packager] ERROR: ingest_root not found: {ingest_root}", file=sys.stderr)
        return 3

    # ---------- Remote CLEAN before publish (explicit) ----------
    if mode in ("github", "both") and clean_before_publish and gh and publish.github_token:
        try:
            github_clean_remote_repo(
                owner=gh.owner,
                repo=gh.repo,
                branch=gh.branch,
                base_path=gh.base_path or "",
                token=publish.github_token,
            )
        except Exception as e:
            print(f"[packager] Publish(GitHub): remote clean failed: {type(e).__name__}: {e}", file=sys.stderr)
            # continue anyway

    result = Packager(cfg, rules=None).run(external_source=ingest_root)
    print(f"Bundle: {result.out_bundle}")
    print(f"Run-spec: {result.out_runspec}")
    print(f"Guide: {result.out_guide}")

    # --- Print GitHub raw URLs for all emitted codebase files -------------------
    if mode in ("github", "both") and gh and (gh.branch is not None):
        emitted_paths = gather_emitted_paths(
            src_dir,
            cfg.emitted_prefix,
            exclude_globs=list(cfg.exclude_globs),
            segment_excludes=list(cfg.segment_excludes),
        )
        print("[packager] GitHub Raw URLs:")
        print_github_raw_urls(gh.owner, gh.repo, gh.branch, gh.base_path or "", emitted_paths)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
