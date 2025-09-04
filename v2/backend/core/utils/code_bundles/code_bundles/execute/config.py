from __future__ import annotations
import os
import yaml
from typing import Dict, Any
from pathlib import Path
from typing import Optional
from types import SimpleNamespace as NS

from v2.backend.core.configuration.loader import (
    get_repo_root,
    get_packager,
    ConfigPaths,
)

class Transport(NS):
    pass

# ──────────────────────────────────────────────────────────────────────────────
# Code-bundle params from vars.yml
# ──────────────────────────────────────────────────────────────────────────────
def read_code_bundle_params() -> Dict[str, Any]:
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

    # (dynamic) read split_bytes from vars.yml (fallback 300000)
    cb_params = read_code_bundle_params()
    split_dyn = int(cb_params.get("split_bytes", 300000) or 300000)

    transport = Transport(
        chunk_bytes=64000,
        chunk_records=True,
        group_dirs=True,
        dir_suffix_width=2,
        parts_per_dir=10,
        part_ext=".txt",
        part_stem="design_manifest",
        parts_index_name="design_manifest_parts_index.json",
        split_bytes=split_dyn,
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