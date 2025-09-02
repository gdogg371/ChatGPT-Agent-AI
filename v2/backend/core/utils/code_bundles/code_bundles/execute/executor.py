# v2/backend/core/utils/code_bundles/code_bundles/executor.py
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from types import SimpleNamespace as NS
from urllib import request, parse, error

# --- use ONLY your split modules ---
from v2.backend.core.utils.code_bundles.code_bundles.execute.config import build_cfg
from v2.backend.core.utils.code_bundles.code_bundles.execute.repo import discover_repo_paths
from v2.backend.core.utils.code_bundles.code_bundles.execute.fs import _clear_dir_contents, copy_snapshot
from v2.backend.core.utils.code_bundles.code_bundles.execute.checksums import _write_sha256sums_for_parts
from v2.backend.core.utils.code_bundles.code_bundles.execute.parts import (
    _write_parts_from_jsonl,
    _append_parts_artifacts_into_manifest,
)
from v2.backend.core.utils.code_bundles.code_bundles.execute.github_api import (
    _gh_headers,
    _gh_json,
    _gh_delete_file,
    _gh_walk_files,
)
from v2.backend.core.utils.code_bundles.code_bundles.execute.publish import (
    github_clean_remote_repo,
    _publish_to_github,
    _prune_remote_code_delta,
    _prune_remote_artifacts_delta,
)
from v2.backend.core.utils.code_bundles.code_bundles.execute.manifest import augment_manifest
from v2.backend.core.utils.code_bundles.code_bundles.execute.emitter import _load_analysis_emitter


# ------------------
# Validation helpers
# ------------------

class ConfigError(RuntimeError):
    pass


def _require(d: dict, key_path: List[str]) -> None:
    cur = d
    for k in key_path:
        if not isinstance(cur, dict) or k not in cur:
            dotted = ".".join(key_path)
            raise ConfigError(f"Missing required config key: {dotted}")
        cur = cur[k]


def _get(d: dict, key_path: List[str]):
    cur = d
    for k in key_path:
        if not isinstance(cur, dict) or k not in cur:
            dotted = ".".join(key_path)
            raise ConfigError(f"Missing required config key: {dotted}")
        cur = cur[k]
    return cur


# ------------------
# GitHub small utils
# ------------------

def _gh_get_file_meta(owner: str, repo: str, path: str, token: str, ref: Optional[str]) -> Optional[dict]:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{parse.quote(path)}"
    if ref:
        url += f"?ref={parse.quote(ref)}"
    req = request.Request(url, headers=_gh_headers(token))
    try:
        meta = _gh_json(req)
        if isinstance(meta, dict) and meta.get("type") == "file":
            return meta
        return None
    except error.HTTPError as e:
        if e.code == 404:
            return None
        raise


# ----------------------------
# Project root auto-discovery
# ----------------------------

def _find_project_root() -> Path:
    """
    Walk upward from CWD to find 'config/packager.yml'.
    If not found, walk upward from this file's directory.
    """
    def search_up(start: Path) -> Optional[Path]:
        start = start.resolve()
        for p in [start] + list(start.parents):
            if (p / "config" / "packager.yml").is_file():
                return p
        return None

    cwd_root = search_up(Path.cwd())
    if cwd_root:
        return cwd_root
    here_root = search_up(Path(__file__).resolve().parent)
    if here_root:
        return here_root
    raise ConfigError("Could not locate 'config/packager.yml' by walking up from CWD or script location.")


# -------------
# Core routine
# -------------

def _run_once(project_root: Path) -> None:
    project_root = Path(project_root).resolve()
    print(f"[executor] project_root = {project_root}")

    # 1) Load config via your module (must read config/packager.yml; no code defaults)
    cfg: NS = build_cfg(project_root=project_root)
    if not hasattr(cfg, "config") or not isinstance(cfg.config, dict):
        raise ConfigError("build_cfg() must return a namespace with a 'config' dict.")

    conf = cfg.config

    # Validate required keys (strict)
    for path in [
        ["emitted_prefix"],
        ["emit_ast"],
        ["include_globs"],
        ["exclude_globs"],
        ["segment_excludes"],
        ["publish"],
        ["publish", "mode"],
        ["publish", "staging_root"],
        ["publish", "output_root"],
        ["publish", "ingest_root"],
        ["publish", "local_publish_root"],
        ["publish", "clean_before_publish"],
        ["publish", "clean"],
        ["publish", "clean", "clean_repo_root"],
        ["publish", "clean", "clean_artifacts"],
        ["publish", "handoff"],
        ["publish", "runspec"],
        ["publish", "transport_index"],
        ["publish", "checksums"],
        ["publish", "github"],
        ["publish", "github", "owner"],
        ["publish", "github", "repo"],
        ["publish", "github", "branch"],
        ["publish", "github", "base_path"],
        ["transport"],
        ["transport", "kind"],
        ["transport", "part_stem"],
        ["transport", "part_ext"],
        ["transport", "parts_per_dir"],
        ["transport", "split_bytes"],
        ["transport", "preserve_monolith"],
        ["metadata_emission"],
        ["analysis_filenames"],
        ["family_aliases"],
        ["controls"],
        ["handoff"],
        ["limits"],
        ["analysis"],
    ]:
        _require(conf, path)

    # Resolve paths from config (NO defaults in code)
    output_root = _get(conf, ["publish", "output_root"])
    local_publish_root = _get(conf, ["publish", "local_publish_root"])

    # Working dirs
    out_root = (project_root / output_root).resolve()
    design_manifest_dir = (out_root / "design_manifest").resolve()
    snapshot_dir = (out_root / "code_snapshot").resolve()

    # Ensure dirs
    design_manifest_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    # Clean before publish (local)
    if _get(conf, ["publish", "clean_before_publish"]):
        print("[executor] clean_before_publish=True → clearing snapshot_dir")
        _clear_dir_contents(snapshot_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)

    if _get(conf, ["publish", "clean", "clean_artifacts"]):
        print("[executor] clean_artifacts=True → clearing design_manifest_dir")
        _clear_dir_contents(design_manifest_dir)
        design_manifest_dir.mkdir(parents=True, exist_ok=True)

    # 2) Discover source files strictly by include/exclude/segment_excludes
    include_globs: List[str] = _get(conf, ["include_globs"])
    exclude_globs: List[str] = _get(conf, ["exclude_globs"])
    segment_excludes: List[str] = _get(conf, ["segment_excludes"])

    repo_files: List[Path] = []
    for p in discover_repo_paths(project_root, include_globs, exclude_globs, segment_excludes):
        try:
            repo_files.append(p.resolve().relative_to(project_root))
        except Exception:
            # Skip anything outside the repo (strict boundary)
            pass
    print(f"[executor] discovered files: {len(repo_files)}")

    # 3) Copy snapshot
    if repo_files:
        copy_snapshot(project_root, snapshot_dir, repo_files)
        print("[executor] snapshot copied")

    # 4) Run optional analysis emitter if present (module decides how to emit)
    emitter = None
    try:
        emitter = _load_analysis_emitter(project_root)
    except TypeError:
        emitter = _load_analysis_emitter()  # tolerate older signature
    if emitter and hasattr(emitter, "run"):
        print("[executor] running analysis_emitter.run()")
        try:
            emitter.run(project_root=project_root, out_root=out_root, config=conf)
        except TypeError:
            emitter.run()

    # 5) Chunk design manifest from JSONL and write checksums (respect config)
    part_stem = _get(conf, ["transport", "part_stem"])
    part_ext = _get(conf, ["transport", "part_ext"])
    parts_per_dir = int(_get(conf, ["transport", "parts_per_dir"]))
    split_bytes = int(_get(conf, ["transport", "split_bytes"]))
    preserve_monolith = bool(_get(conf, ["transport", "preserve_monolith"]))

    jsonl_path = (project_root / output_root / "design_manifest.jsonl").resolve()
    parts_written: List[Path] = []
    sums_file: Optional[Path] = None

    if jsonl_path.exists():
        print(f"[executor] writing parts from JSONL: {jsonl_path}")
        parts_written = _write_parts_from_jsonl(
            jsonl_path=jsonl_path,
            parts_dir=design_manifest_dir,
            part_stem=part_stem,
            part_ext=part_ext,
            split_bytes=split_bytes,
            parts_per_dir=parts_per_dir,
            preserve_monolith=preserve_monolith,
        )
        if _get(conf, ["publish", "checksums"]):
            sums_file = design_manifest_dir / f"{part_stem}.SHA256SUMS"
            _write_sha256sums_for_parts(design_manifest_dir, sums_file)
            print(f"[executor] checksums → {sums_file.name}")
    else:
        print(f"[executor] WARNING: missing JSONL: {jsonl_path}")

    # 6) Manifest augmentation (strictly via module)
    manifest: dict = {}
    if parts_written:
        manifest = _append_parts_artifacts_into_manifest(
            manifest=manifest,
            parts_written=parts_written,
            parts_dir=design_manifest_dir,
            sums_file=sums_file,
        )

    manifest = augment_manifest(cfg, manifest)
    final_manifest_path = design_manifest_dir / "manifest.json"
    final_manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[executor] manifest → {final_manifest_path}")

    # 7) Local publish mirror
    mode = str(_get(conf, ["publish", "mode"])).lower()
    if mode in ("local", "both"):
        local_root = (project_root / local_publish_root).resolve()
        (local_root / "design_manifest").mkdir(parents=True, exist_ok=True)
        (local_root / "code_snapshot").mkdir(parents=True, exist_ok=True)

        def _mirror(src: Path, dst: Path):
            for p in src.rglob("*"):
                if p.is_file():
                    out = dst / p.relative_to(src)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(p.read_bytes())

        print(f"[executor] local publish → {local_root}")
        _mirror(design_manifest_dir, local_root / "design_manifest")
        _mirror(snapshot_dir, local_root / "code_snapshot")

    # 8) GitHub publish (clean/prune as per config)
    if mode in ("github", "both"):
        gh_owner = _get(conf, ["publish", "github", "owner"]).strip()
        gh_repo = _get(conf, ["publish", "github", "repo"]).strip()
        gh_branch = _get(conf, ["publish", "github", "branch"]).strip()
        gh_base = _get(conf, ["publish", "github", "base_path"]).strip()

        # Token is expected to be provided by build_cfg (merged from secrets); do NOT invent defaults.
        gh_token = getattr(cfg, "github_token", None)
        if not gh_token:
            raise ConfigError("GitHub token not found in cfg.github_token (secrets must provide it).")

        clean_repo_root = bool(_get(conf, ["publish", "clean", "clean_repo_root"]))

        if clean_repo_root:
            print(f"[executor] github clean under '{gh_base or '/'}'")
            github_clean_remote_repo(gh_owner, gh_repo, gh_base, gh_token)
        else:
            # Prune stale files deterministically
            remote_all = _gh_walk_files(gh_owner, gh_repo, gh_base or "", gh_token) or []

            # Local listings
            local_code = [
                str((gh_base + "/" if gh_base else "") + (snapshot_dir / p).relative_to(snapshot_dir).as_posix())
                for p in snapshot_dir.rglob("*") if p.is_file()
            ]
            local_artifacts = [
                str(p.relative_to(design_manifest_dir).as_posix())
                for p in design_manifest_dir.rglob("*") if p.is_file()
            ]
            # Compute deletions
            dels_code = _prune_remote_code_delta(cfg, remote_all, local_code)
            dels_art = _prune_remote_artifacts_delta(cfg, remote_all, local_artifacts)
            to_delete = sorted(set(dels_code) | set(dels_art))

            if to_delete:
                print(f"[executor] github prune deletions: {len(to_delete)}")
                for path in to_delete:
                    meta = _gh_get_file_meta(gh_owner, gh_repo, path, gh_token, gh_branch)
                    if meta and "sha" in meta:
                        _gh_delete_file(gh_owner, gh_repo, path, meta["sha"], gh_token, f"prune: remove {path}")

        # Publish code snapshot
        print(f"[executor] github publish code → {gh_base or '/'}")
        _publish_to_github(cfg, snapshot_dir, gh_base)

        # Publish design_manifest under base/design_manifest
        artifacts_base = f"{gh_base}/design_manifest" if gh_base else "design_manifest"
        print(f"[executor] github publish artifacts → {artifacts_base}")
        _publish_to_github(cfg, design_manifest_dir, artifacts_base)

    print("[executor] DONE")


def main() -> None:
    root = _find_project_root()
    _run_once(root)


if __name__ == "__main__":
    main()
