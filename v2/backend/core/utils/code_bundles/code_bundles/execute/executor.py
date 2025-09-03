from __future__ import annotations
import sys
import inspect
from pathlib import Path
from typing import Optional



from v2.backend.core.utils.code_bundles.code_bundles.src.packager.core.orchestrator import Packager
#from v2.backend.core.utils.code_bundles.code_bundles.src.packager.analysis_emitter import emit_all

import importlib.util
from importlib.machinery import SourceFileLoader

def _load_analysis_emitter(cfg):
    try:
        from v2.backend.core.utils.code_bundles.code_bundles.src.packager.analysis_emitter import emit_all as _emit
        return _emit
    except Exception:
        pass
    here = Path(__file__).parent
    cand = here / "src" / "packager" / "analysis_emitter.py"
    if cand.exists():
        spec = importlib.util.spec_from_loader("analysis_emitter", SourceFileLoader("analysis_emitter", str(cand)))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        return getattr(mod, "emit_all", None)
    if hasattr(cfg, "source_root"):
        cand2 = Path(cfg.source_root) / "v2" / "backend" / "core" / "utils" / "code_bundles" / "code_bundles" / "src" / "packager" / "analysis_emitter.py"
        if cand2.exists():
            spec = importlib.util.spec_from_loader("analysis_emitter", SourceFileLoader("analysis_emitter", str(cand2)))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore
            return getattr(mod, "emit_all", None)
    print("[packager] analysis_emitter import failed → emitter unavailable", flush=True)
    return None

import v2.backend.core.utils.code_bundles.code_bundles.src.packager.core.orchestrator as orch_mod
from v2.backend.core.utils.code_bundles.code_bundles.execute.funcs import (
read_root_emit_ast,
read_root_publish_analysis,
clear_dir_contents,
discover_repo_paths,
copy_snapshot
)
from v2.backend.core.utils.code_bundles.code_bundles.execute.read_scanners import (
augment_manifest
)
from v2.backend.core.utils.code_bundles.code_bundles.execute.manifest import (
maybe_chunk_manifest_and_update
)
from v2.backend.core.utils.code_bundles.code_bundles.execute.config import (
    build_cfg
)
from v2.backend.core.utils.code_bundles.code_bundles.execute.github import (
github_clean_remote_repo,
publish_to_github,
prune_remote_code_delta,
prune_remote_artifacts_delta,
print_full_raw_links
)
from v2.backend.core.configuration.loader import (
    get_repo_root,
    get_packager,
    get_secrets,
    ConfigError,
    ConfigPaths,
)
from v2.backend.core.utils.code_bundles.code_bundles.bundle_io import (
    rewrite_manifest_paths,
    write_sha256sums_for_file,
)

from v2.backend.core.utils.code_bundles.code_bundles.telemetry.runtime_flow import FlowLogger
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.io.guide_writer import GuideWriter

# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
def main() -> int:
    # Disable legacy per-file sums for the whole run (emitter writes canonical sums)
    #import os
    #os.environ.setdefault("PACKAGER_DISABLE_LEGACY_SUMS", "1")

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
    root_publish_analysis = read_root_publish_analysis()
    root_emit_ast = read_root_emit_ast()
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

    # Initialize runtime flow logger to write alongside manifest files

        publish_analysis=root_publish_analysis,  # <- root-level only
        publish_handoff=bool(pub.get("publish_handoff", True)),
        publish_transport=bool(pub.get("publish_transport", True)),
        local_publish_root=None,
        clean_before_publish=bool(clean_artifacts) if (do_github and not clean_repo_root) else False,
        emit_ast=root_emit_ast,  # <- root-level only
    )

    flow = FlowLogger(log_path=(Path(cfg.out_bundle).parent / "run_events.jsonl"))
    flow.begin_run(meta={"argv": sys.argv, "cwd": str(Path.cwd())})

    print(f"[packager] using orchestrator from: {inspect.getsourcefile(orch_mod) or '?'}")
    print(f"[packager] source_root: {cfg.source_root}")
    print(f"[packager] emitted_prefix: {cfg.emitted_prefix}")
    print(f"[packager] include_globs: {list(cfg.include_globs)}")
    print(f"[packager] exclude_globs: {list(cfg.exclude_globs)}")
    print(f"[packager] segment_excludes: {list(cfg.segment_excludes)}")
    print(f"[packager] follow_symlinks: {cfg.follow_symlinks} case_insensitive: {cfg.case_insensitive}")
    print("[packager] Packager: start]")

    if do_local:
        clear_dir_contents(artifact_root)
        clear_dir_contents(code_output_root)

    with flow.phase("packager.run", step=10):
        result = Packager(cfg, rules=None).run(external_source=None)
    print(f"Bundle: {result.out_bundle}")
    print(f"Run-spec: {result.out_runspec}")
    print(f"Guide: {result.out_guide}")

    with flow.phase("discover.repo", step=20):
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
        with flow.phase("snapshot.local", step=30, files=len(discovered_repo)):
            copied = copy_snapshot(discovered_repo, code_output_root)
        print(f"[packager] Local snapshot: copied {copied} files to {code_output_root}")

    # LOCAL augment
    if do_local:
        with flow.phase("augment.local", step=40):
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
            with flow.phase("augment.github", step=41):
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
        with flow.phase("chunk.local", step=50):
            rep = maybe_chunk_manifest_and_update(cfg=cfg, which="local")
        print(f"[packager] chunk report (local): {rep}")

    if do_github and gh_manifest_override and gh_manifest_override.exists():
        local_bundle, local_sums = cfg.out_bundle, cfg.out_sums
        try:
            cfg.out_bundle = gh_manifest_override
            cfg.out_sums = gh_sums_override or (gh_manifest_override.parent / "design_manifest.github.SHA256SUMS")
            with flow.phase("chunk.github", step=51):
                rep = maybe_chunk_manifest_and_update(cfg=cfg, which="github")
            print(f"[packager] chunk report (github): {rep}")
        finally:
            cfg.out_bundle, cfg.out_sums = local_bundle, local_sums

    if do_local and Path(cfg.out_bundle).exists():
        write_sha256sums_for_file(target_file=Path(cfg.out_bundle), out_sums_path=Path(cfg.out_sums))

    # ---- emit analysis sidecars & canonical checksums before publish ----
    _emit_analysis_sidecars = _load_analysis_emitter(cfg)
    print(
        f"[packager] publish_analysis (emitter gate): {getattr(cfg, 'publish_analysis', None)}  emitter={'set' if _emit_analysis_sidecars else 'none'}",
        flush=True)
    if getattr(cfg, "publish_analysis", True) and _emit_analysis_sidecars:
        print("[packager] Emitting analysis sidecars...", flush=True)
        with flow.phase("analysis.emit", step=60):
            _emit_analysis_sidecars(repo_root=Path(cfg.source_root).resolve(), cfg=cfg)

    # (added) Write assistant handoff after chunking + analysis, before publish
    with flow.phase("handoff.write", step=65):
        GuideWriter(Path(cfg.out_guide)).write(cfg=cfg)

    # GitHub publish (includes analysis/** when root-level flag is true)
    if do_github:
        if clean_repo_root:
            try:
                github_clean_remote_repo(owner=gh_owner, repo=gh_repo, branch=gh_branch, base_path="", token=str(gh_token))
            except Exception as e:
                print(f"[packager] WARN: full repo clean failed: {type(e).__name__}: {e}")

            with flow.phase("publish.github", step=70):
                publish_to_github(
                cfg=cfg,
                code_items_repo_rel=discovered_repo,
                base_path=gh_base,
                manifest_override=gh_manifest_override if (gh_manifest_override and gh_manifest_override.exists()) else None,
                sums_override=gh_sums_override if (gh_sums_override and gh_sums_override.exists()) else None,
            )
            print_full_raw_links(gh_owner, gh_repo, gh_branch, str(gh_token))
        else:
            with flow.phase("publish.github", step=70):
                publish_to_github(
                cfg=cfg,
                code_items_repo_rel=discovered_repo,
                base_path=gh_base,
                manifest_override=gh_manifest_override if (gh_manifest_override and gh_manifest_override.exists()) else None,
                sums_override=gh_sums_override if (gh_sums_override and gh_sums_override.exists()) else None,
            )
            try:
                with flow.phase("prune.github.code", step=80):
                    deleted_code = prune_remote_code_delta(
                    cfg=cfg,
                    gh_owner=gh_owner,
                    gh_repo=gh_repo,
                    gh_branch=gh_branch,
                    token=str(gh_token),
                    discovered_repo=discovered_repo,
                    base_path=gh_base,
                )
                with flow.phase("prune.github.artifacts", step=81):
                    deleted_art = prune_remote_artifacts_delta(
                    cfg=cfg,
                    gh_owner=gh_owner,
                    gh_repo=gh_repo,
                    gh_branch=gh_branch,
                    token=str(gh_token),
                )
                print(f"[packager] Delta prune: code={deleted_code}, artifacts={deleted_art}")
            finally:
                print_full_raw_links(gh_owner, gh_repo, gh_branch, str(gh_token))

    print("[packager] done.")
    try:
        flow.end_run(status="ok")
    except Exception:
        pass
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



