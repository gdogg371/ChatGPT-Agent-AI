from __future__ import annotations
import os
import yaml
from typing import Dict, Any, Optional, List
from pathlib import Path
from types import SimpleNamespace as NS

from v2.backend.core.utils.code_bundles.code_bundles.execute.loader import (
    get_repo_root,
    get_packager,
)


class ConfigError(RuntimeError):
    pass


class Transport(NS):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# YAML loaders
# ──────────────────────────────────────────────────────────────────────────────

def _load_packager_config(repo_root: Path) -> Dict[str, Any]:
    cfg_path = Path(repo_root) / "config" / "packager.yml"
    if not cfg_path.exists():
        return {}
    with cfg_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"config/packager.yml must be a mapping, got {type(data).__name__}")
    return data


def _load_packager_transport(repo_root: Path) -> Dict[str, Any]:
    yml = _load_packager_config(repo_root)
    t = yml.get("transport") or {}
    if not isinstance(t, dict):
        raise ConfigError("transport section in packager.yml must be a mapping")
    return t


# ──────────────────────────────────────────────────────────────────────────────
# Build runtime config
# ──────────────────────────────────────────────────────────────────────────────

def build_cfg(
    *,
    src: Path,
    artifact_out: Path,
    publish_mode: str,
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
    clean_before_publish: bool = False,
    emit_ast: bool = False,
) -> NS:
    pack = get_packager()
    repo_root = get_repo_root()

    # Artifact filenames (under artifact_out)
    out_bundle = (artifact_out / "design_manifest.jsonl").resolve()
    out_runspec = (artifact_out / "superbundle.run.json").resolve()
    out_guide = (artifact_out / "assistant_handoff.v1.json").resolve()
    out_sums = (artifact_out / "design_manifest.SHA256SUMS").resolve()

    # Load YAML (preferred single source of truth)
    yml = _load_packager_config(repo_root)
    mp: Dict[str, Any] = dict(yml.get("manifest_paths") or {})
    af: Dict[str, Any] = dict(yml.get("analysis_filenames") or {})
    pub_map: Dict[str, Any] = dict(yml.get("publish") or {})
    gh_map: Dict[str, Any] = dict(pub_map.get("github") or {})
    pt_map: Dict[str, Any] = _load_packager_transport(repo_root)

    # Transport from YAML (required keys)
    required_t = ["part_stem", "part_ext", "parts_per_dir", "split_bytes", "preserve_monolith"]
    missing_t = [k for k in required_t if k not in pt_map]
    if missing_t:
        raise ConfigError(f"transport missing required keys: {', '.join(missing_t)}")

    transport = Transport(
        # helpers (unchanged)
        chunk_bytes=64000,
        chunk_records=True,
        dir_suffix_width=int(pt_map.get("dir_suffix_width", 2)),
        transport_as_text=True,
        # core
        part_stem=str(pt_map["part_stem"]),
        part_ext=str(pt_map["part_ext"]),
        parts_per_dir=int(pt_map["parts_per_dir"]),
        split_bytes=int(pt_map["split_bytes"]),
        preserve_monolith=bool(pt_map["preserve_monolith"]),
        # indices/exts
        parts_index_name=f'{pt_map["part_stem"]}_parts_index.json',
        monolith_ext=str(pt_map.get("monolith_ext", ".jsonl")),
        group_dirs=bool(pt_map.get("group_dirs", True)),
    )

    # GitHub publish block (args take precedence, else YAML)
    gh_owner = gh_owner or gh_map.get("owner")
    gh_repo = gh_repo or gh_map.get("repo")
    gh_branch = gh_branch or gh_map.get("branch", "main")
    gh_base = gh_base or gh_map.get("base_path", "") or gh_map.get("base", "")

    gh = None
    if gh_owner and gh_repo:
        gh = NS(owner=gh_owner, repo=gh_repo, branch=gh_branch, base_path=gh_base)

    mode = (publish_mode or "local").lower()

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
    # Pass through filenames from YAML so writers don't hardcode
    setattr(publish, "runspec_filename", pub_map.get("runspec_filename"))
    setattr(publish, "handoff_filename", pub_map.get("handoff_filename"))

    # Compute analysis_out_dir from YAML analysis_subdir (default 'analysis')
    analysis_subdir = str(mp.get("analysis_subdir") or "analysis")
    analysis_out_dir = (artifact_out / analysis_subdir).resolve()

    # Pull packager fields with safe defaults
    emitted_prefix = getattr(pack, "emitted_prefix", ".")
    include_globs: List[str] = list(getattr(pack, "include_globs", ["**/*"]))
    exclude_globs: List[str] = list(getattr(pack, "exclude_globs", []))
    segment_excludes: List[str] = list(getattr(pack, "segment_excludes", []))
    follow_symlinks = bool(getattr(pack, "follow_symlinks", True))
    case_insensitive = bool(getattr(pack, "case_insensitive", True))

    cfg = NS(
        source_root=Path(src).resolve(),
        emitted_prefix=emitted_prefix,
        include_globs=include_globs,
        exclude_globs=exclude_globs,
        follow_symlinks=follow_symlinks,
        case_insensitive=case_insensitive,
        segment_excludes=segment_excludes,
        out_bundle=out_bundle,
        out_runspec=out_runspec,
        out_guide=out_guide,
        out_sums=out_sums,
        transport=transport,
        publish=publish,
        prompts=None,
        prompt_mode="none",
        emit_ast=bool(emit_ast),

        # expose YAML sections for downstream writers
        manifest_paths=mp,
        analysis_filenames=af,
        analysis_out_dir=analysis_out_dir,
    )

    # Public prompts (empty by default)
    cfg.prompts_public = {}

    # Provenance (best-effort)
    cfg.packager_version = getattr(pack, "version", None) or getattr(pack, "packager_version", None)
    cfg.packager_git_sha = (
        getattr(pack, "git_sha", None)
        or getattr(pack, "code_sha", None)
        or getattr(pack, "repo_sha", None)
    )

    # Ensure artifact directory exists
    artifact_out.mkdir(parents=True, exist_ok=True)
    return cfg
