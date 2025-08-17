# run_pack.py
from __future__ import annotations
from pathlib import Path
from typing import Optional, Tuple
import json
import shutil

import normalize as norm

# Imports from your package path
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.core.config import (
    PackConfig, Limits, Policy, SandboxConstraints, ExecutionPolicy, PromptSource,
    TransportOptions, PublishOptions, GitHubPublish
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.core.orchestrator import Packager


# ------------------- repo-local & home creds locations -------------------
SECRETS_CFG = Path(r"C:\Users\cg371\PycharmProjects\ChatGPT Bot\secrets\publish.local.json")
HOME_CFG    = Path.home() / ".config" / "packager" / "publish.local.json"


def load_publish_config() -> PublishOptions:
    """
    Load publishing configuration from repo-local secrets first, then home config.
    Falls back to 'local' mode if not found or incomplete.
    """
    cfg_path = SECRETS_CFG if SECRETS_CFG.exists() else HOME_CFG if HOME_CFG.exists() else None
    if not cfg_path:
        return PublishOptions(mode="local", local_publish_root=None)

    cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
    gh = cfg_data.get("github") or {}

    opts = PublishOptions(
        mode=cfg_data.get("mode", "local"),
        github=GitHubPublish(
            owner=gh.get("owner", ""),
            repo=gh.get("repo", ""),
            branch=gh.get("branch", "main"),
            base_path=gh.get("base_path", "")
        ) if gh else None,
        github_token=cfg_data.get("github_token", ""),
        local_publish_root=Path(cfg_data["local_publish_root"]) if cfg_data.get("local_publish_root") else None,
        publish_codebase=bool(cfg_data.get("publish_codebase", True)),
        publish_analysis=bool(cfg_data.get("publish_analysis", True)),
        publish_handoff=bool(cfg_data.get("publish_handoff", True)),
        publish_transport=bool(cfg_data.get("publish_transport", False)),
        publish_prompts=bool(cfg_data.get("publish_prompts", True)),
    )

    # Guardrail: if GitHub selected but token/coords missing, fall back to local
    if opts.mode in ("github", "both"):
        if not opts.github or not opts.github.owner or not opts.github.repo or not opts.github_token:
            print("[packager] WARN: GitHub publish selected but token/coords missing; falling back to local.")
            return PublishOptions(mode="local", local_publish_root=opts.local_publish_root)
    return opts


# ------------------- output & mirror locations -------------------
OUT_DIR     = Path(r"C:\Users\cg371\PycharmProjects\ChatGPT Bot\v2\patches\output\design_manifest")
OUT_BUNDLE  = OUT_DIR / "design_manifest.jsonl"
OUT_SUMS    = OUT_DIR / "design_manifest.SHA256SUMS"
RUN_SPEC    = OUT_DIR / "superbundle.run.json"
GUIDE_PATH  = OUT_DIR / "assistant_handoff.v1.json"

# Mirror destination for the external source (where code is copied to)
SOURCE_ROOT = Path(r"C:\Users\cg371\PycharmProjects\ChatGPT Bot\v2\patches\output\patch_code_bundles")

# External source you want mirrored (repo root)
EXTERNAL_SOURCE = Path(r"C:\\Users\\cg371\\PycharmProjects\\ChatGPT Bot\\v2\\")


# ------------------- transport profile (no env vars) -------------------
PACKAGER_SPLIT_BYTES = 300_000     # ~300 KB per part
PACKAGER_CHUNK_BYTES = 64_000      # ~64 KB per JSONL 'file_chunk'
TRANSPORT = TransportOptions(
    transport_as_text=True,                    # parts use .txt extension; lines remain JSONL
    chunk_records=True,                        # emit 'file_chunk' records to keep lines short
    chunk_bytes=PACKAGER_CHUNK_BYTES,          # raw bytes per chunk
    split_bytes=PACKAGER_SPLIT_BYTES,          # target bytes per part
    preserve_monolith=False,                   # remove monolithic design_manifest.jsonl after splitting
    part_stem="design_manifest",
    part_ext=".txt",
    parts_index_name="design_manifest_parts_index.json",
    # grouping hints (supported by your TransportOptions)
    group_dirs=True,
    parts_per_dir=10,
    dir_suffix_width=2,
    upload_batch_hint=10,                      # UI tip for how many files to drag at once
)


# ------------------- normalization baseline -------------------
NORMALIZE = norm.NormalizationRules(
    newline_policy="lf", encoding="utf-8", strip_trailing_ws=True, excluded_paths=tuple()
)


# ------------------- ingestion hardening (globs) -------------------
EXCLUDE_GLOBS = (
    # your secrets folder and the creds file
    "**/secrets/**",
    "**/publish.local.json",
    # typical noise and heavy dirs
    "**/.git/**", "**/node_modules/**", "**/dist/**", "**/build/**", "**/output/**", "**/software/**",
    "**/__pycache__/**", "**/*.pyc",
    # exclude this packager subtree if it lives under the same repo root
    "**/backend/core/utils/code_bundles/code_bundles/**",
)


# ------------------- helpers -------------------
def _clear_dir(p: Path) -> None:
    """Delete all contents of a directory (if exists) and recreate it."""
    if p.exists():
        for child in p.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)  # type: ignore[arg-type]
            except Exception:
                # best-effort cleanup
                pass
    p.mkdir(parents=True, exist_ok=True)


# ------------------- main -------------------
def main(
    limits: Optional[Limits] = None,
    include_globs: Tuple[str, ...] = (),
    exclude_globs: Tuple[str, ...] = EXCLUDE_GLOBS,
    prompts: Optional[PromptSource] = None,
    sandbox_caps: Optional[Tuple[Optional[int], Optional[int], Optional[int]]] = None,
    external_source: Optional[Path] = EXTERNAL_SOURCE
) -> int:
    # Pre-run cleanup (mirror + design manifest output)
    print(f"[run_pack] Clearing mirror: {SOURCE_ROOT}")
    _clear_dir(SOURCE_ROOT)
    print(f"[run_pack] Clearing manifest out: {OUT_DIR}")
    _clear_dir(OUT_DIR)

    # Sandbox policy setup
    max_cpu_seconds = max_memory_mb = timeout_seconds_per_run = None
    if sandbox_caps:
        max_cpu_seconds, max_memory_mb, timeout_seconds_per_run = sandbox_caps

    policy = Policy(
        sandbox_constraints=SandboxConstraints(
            offline_only=True,
            max_cpu_seconds=max_cpu_seconds,
            max_memory_mb=max_memory_mb,
            timeout_seconds_per_run=timeout_seconds_per_run,
        ),
        execution_policy=ExecutionPolicy(
            require_attempt=True,
            phases=("on_intake", "end_of_dev_cycle"),
        ),
    )

    # Load publish options (GitHub/local) from secrets path (or home)
    publish = load_publish_config()

    # Build config for the packager
    cfg = PackConfig(
        source_root=SOURCE_ROOT,
        out_bundle=OUT_BUNDLE,
        out_sums=OUT_SUMS,
        out_runspec=RUN_SPEC,
        out_guide=GUIDE_PATH,
        emitted_prefix="codebase/",
        include_globs=include_globs,
        exclude_globs=exclude_globs,
        limits=limits,
        policy=policy,
        prompts=prompts,
        prompt_mode=("embed" if prompts is not None else "omit"),
        follow_symlinks=False,
        transport=TRANSPORT,
        publish=publish,   # ← from publish.local.json (secrets/) or home fallback
    )

    # Run the packager
    Packager(cfg, rules=NORMALIZE).run(external_source=external_source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
