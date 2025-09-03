from __future__ import annotations

import fnmatch
import json
import os
import sys
import traceback
from hashlib import sha256
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from types import SimpleNamespace as NS

# ---- split modules already in your tree ----
from v2.backend.core.utils.code_bundles.code_bundles.execute.config import build_cfg
from v2.backend.core.utils.code_bundles.code_bundles.execute.manifest import augment_manifest
from v2.backend.core.utils.code_bundles.code_bundles.execute.emitter import _load_analysis_emitter
from v2.backend.core.utils.code_bundles.code_bundles.execute.fs import (
    _clear_dir_contents,
    copy_snapshot,
)

# The JSONL appender + standard artifact emitters, same as run_pack
from v2.backend.core.utils.code_bundles.code_bundles.bundle_io import (
    ManifestAppender,
    emit_standard_artifacts,
)

# ============================================================================
#// Minimal logger that matches the old "[packager]" style
# ============================================================================
def log(msg: str) -> None:
    print(f"[packager] {msg}", flush=True)

# ============================================================================
#// Root discovery (locates config/packager.yml by walking upward)
# ============================================================================
class ConfigError(RuntimeError):
    pass

def _find_project_root() -> Path:
    def search_up(start: Path) -> Optional[Path]:
        start = start.resolve()
        for p in [start] + list(start.parents):
            if (p / "config" / "packager.yml").is_file():
                return p
        return None
    got = search_up(Path.cwd()) or search_up(Path(__file__).resolve().parent)
    if not got:
        raise ConfigError("Could not locate config/packager.yml by walking up from CWD or script folder.")
    return got

# ============================================================================
#// Discovery helpers (ported behavior)
# ============================================================================
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

def _discover_repo_pairs(
    *,
    repo_root: Path,
    include_globs: List[str],
    exclude_globs: List[str],
    segment_excludes: List[str],
    case_insensitive: bool,
    follow_symlinks: bool,
) -> List[Tuple[Path, str]]:
    out: List[Tuple[Path, str]] = []
    for cur, dirs, files in os.walk(repo_root, followlinks=follow_symlinks):
        pruned_dirs = []
        for d in dirs:
            try:
                parts = (Path(cur) / d).relative_to(repo_root).parts
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
            rel_posix = p.relative_to(repo_root).as_posix()
            if include_globs and not _match_any(rel_posix, include_globs, case_insensitive):
                continue
            if exclude_globs and _match_any(rel_posix, exclude_globs, case_insensitive):
                continue
            out.append((p, rel_posix))
    out.sort(key=lambda t: t[1])
    return out

# ============================================================================
#// Chunker + checksums (binary-safe)
# ============================================================================
def _write_parts_from_jsonl(
    *,
    jsonl_path: Path,
    parts_dir: Path,
    part_stem: str,
    part_ext: str,
    split_bytes: int,
    parts_per_dir: int,
) -> List[Path]:
    parts_dir.mkdir(parents=True, exist_ok=True)
    if not jsonl_path.exists():
        log(f"chunk(local): JSONL not found → {jsonl_path}")
        return []

    def _part_name(dir_idx: int, file_idx: int) -> str:
        return f"{part_stem}_{dir_idx:02d}_{file_idx:04d}{part_ext}"

    dir_idx = 0
    file_idx = 0
    cur_bytes = 0
    cur_lines = 0
    cur_files_in_dir = 0
    buf = bytearray()
    out_paths: List[Path] = []

    def flush():
        nonlocal buf, cur_bytes, cur_lines, file_idx, dir_idx, cur_files_in_dir
        if cur_lines == 0:
            return
        subdir = parts_dir / f"{dir_idx:02d}" if parts_per_dir > 0 else parts_dir
        subdir.mkdir(parents=True, exist_ok=True)
        file_idx += 1
        out_path = subdir / _part_name(dir_idx, file_idx)
        out_path.write_bytes(bytes(buf))
        out_paths.append(out_path)
        cur_bytes = 0
        cur_lines = 0
        buf = bytearray()
        cur_files_in_dir += 1
        if parts_per_dir > 0 and cur_files_in_dir >= parts_per_dir:
            dir_idx += 1
            cur_files_in_dir = 0

    with open(jsonl_path, "rb") as f:
        while True:
            chunk = f.readline()
            if not chunk:
                break
            line_len = len(chunk)
            if cur_lines > 0 and (cur_bytes + line_len) > split_bytes:
                flush()
            buf.extend(chunk)
            cur_bytes += line_len
            cur_lines += 1
        flush()

    idx_path = parts_dir / f"{part_stem}_parts_index.json"
    index = {
        "format": "jsonl.parts.v1",
        "parts": [str(p.relative_to(parts_dir).as_posix()) for p in out_paths],
        "split_bytes": split_bytes,
        "parts_per_dir": parts_per_dir,
    }
    idx_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return out_paths

def _write_sha256sums_for_parts(parts_dir: Path, part_stem: str, part_ext: str, sums_path: Path) -> int:
    sums_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    # include index if present
    for p in sorted(parts_dir.rglob(f"{part_stem}_parts_index.json")):
        dg = sha256(p.read_bytes()).hexdigest()
        lines.append(f"{dg}  {p.name}\n")
    # include parts
    for p in sorted(parts_dir.rglob(f"{part_stem}_*_*{part_ext}")):
        if p.is_file():
            dg = sha256(p.read_bytes()).hexdigest()
            lines.append(f"{dg}  {p.name}\n")
    if lines:
        sums_path.write_text("".join(lines), encoding="utf-8")
    return len(lines)

# ============================================================================
#// Summaries to mimic the run_pack counters (best-effort)
# ============================================================================
def _summarize_analysis(analysis: Dict[str, List[dict]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for fam, items in (analysis.items() if isinstance(analysis, dict) else []):
        out[fam] = len(items) if isinstance(items, list) else 0
    return out

def _orchestrator_hint(repo_root: Path) -> str:
    guess = repo_root / "v2" / "backend" / "core" / "utils" / "code_bundles" / "code_bundles" / "src" / "packager" / "core" / "orchestrator.py"
    return str(guess) if guess.exists() else "(missing) " + str(guess)

# ============================================================================
#// main
# ============================================================================
def main() -> None:
    try:
        # Locate project + read YAML
        repo_root = _find_project_root()
        cfg = build_cfg(project_root=repo_root)
        conf = cfg.config

        # Modes
        mode = str(conf.get("publish", {}).get("mode", "both")).lower()
        mode_local = mode in ("local", "both")
        mode_github = mode in ("github", "both")

        # header log (match your old run_pack)
        log(f"mode: {mode} (local={mode_local}, github={mode_github})")
        log(f"publish_analysis (root-level): {bool(conf.get('publish_analysis', False))}")
        log(f"emit_ast (root-level): {bool(conf.get('emit_ast', False))}")
        log(f"using orchestrator from: {_orchestrator_hint(repo_root)}")

        # Config essentials
        include_globs = list(conf.get("include_globs", []))
        exclude_globs = list(conf.get("exclude_globs", []))
        segment_excludes = list(conf.get("segment_excludes", []))
        emitted_prefix = str(conf.get("emitted_prefix", "output/patch_code_bundles")).strip("/")
        case_insensitive = (os.name == "nt")
        follow_symlinks = True

        # Output locations (hard targets from run_pack)
        artifact_root = (repo_root / "output" / "design_manifest").resolve()
        code_root = (repo_root / emitted_prefix).resolve()

        # Respect YAML clean flags
        clean_before = bool(conf.get("publish", {}).get("clean_before_publish", False))
        clean_artifacts = bool(conf.get("publish", {}).get("clean", {}).get("clean_artifacts", False))

        if clean_before:
            log(f"clean_before_publish=True → clearing {code_root}")
            _clear_dir_contents(code_root)
        else:
            log("clean_before_publish=False → NOT clearing code snapshot directory")

        if clean_artifacts:
            log(f"clean_artifacts=True → clearing {artifact_root}")
            _clear_dir_contents(artifact_root)
        else:
            log("clean_artifacts=False → NOT clearing artifact directory")

        # Ensure dirs exist after potential clean
        artifact_root.mkdir(parents=True, exist_ok=True)
        code_root.mkdir(parents=True, exist_ok=True)

        out_bundle = artifact_root / "design_manifest.jsonl"
        out_runspec = artifact_root / "superbundle.run.json"
        out_guide = artifact_root / "assistant_handoff.v1.json"
        out_sums = artifact_root / "design_manifest.SHA256SUMS"

        # paint cfg for any modules that peek into it
        cfg.source_root = repo_root
        cfg.emitted_prefix = emitted_prefix
        cfg.include_globs = include_globs
        cfg.exclude_globs = exclude_globs
        cfg.segment_excludes = segment_excludes
        cfg.case_insensitive = case_insensitive
        cfg.follow_symlinks = follow_symlinks
        tcfg = conf.get("transport", {}) or {}
        cfg.transport = NS(
            part_stem=str(tcfg.get("part_stem", "design_manifest")),
            part_ext=str(tcfg.get("part_ext", ".txt")),
            parts_per_dir=int(tcfg.get("parts_per_dir", 10)),
            split_bytes=int(tcfg.get("split_bytes", 150000)),
            preserve_monolith=bool(tcfg.get("preserve_monolith", False)),
        )
        cfg.out_bundle = out_bundle
        cfg.out_runspec = out_runspec
        cfg.out_guide = out_guide
        cfg.out_sums = out_sums

        # More header, matching your example
        log(f"source_root: {repo_root}")
        log(f"emitted_prefix: {emitted_prefix}")
        log(f"include_globs: {include_globs}")
        log(f"exclude_globs: {exclude_globs}")
        log(f"segment_excludes: {segment_excludes}")
        log(f"follow_symlinks: {follow_symlinks} case_insensitive: {case_insensitive}")
        log("Packager: start]")
        print(f"Bundle: {out_bundle}")
        print(f"Run-spec: {out_runspec}")
        print(f"Guide: {out_guide}")

        # Discover + snapshot
        discovered = _discover_repo_pairs(
            repo_root=repo_root,
            include_globs=include_globs,
            exclude_globs=exclude_globs,
            segment_excludes=segment_excludes,
            case_insensitive=case_insensitive,
            follow_symlinks=follow_symlinks,
        )
        log(f"discovered repo files: {len(discovered)}")
        copied = copy_snapshot(repo_root, code_root, discovered) if mode_local or mode_github else 0
        log(f"Local snapshot: copied {copied} files to {code_root}")

        # Start manifest (JSONL header artifacts)
        # Ensure we don't append to a previous run's JSONL
        if out_bundle.exists():
            log("existing bundle detected → removing to avoid append")
            out_bundle.unlink(missing_ok=True)

        app = ManifestAppender(out_bundle)
        emit_standard_artifacts(
            appender=app,
            out_bundle=out_bundle,
            out_sums=out_sums,
            out_runspec=out_runspec,
            out_guide=out_guide,
        )

        # Augment (returns dict); write rows to JSONL
        log("Augment manifest: start (path_mode=local)")
        manifest_dict: dict = augment_manifest(cfg, {})
        analysis: Dict[str, List[dict]] = manifest_dict.get("analysis", {}) or {}
        fam_counts = _summarize_analysis(analysis)

        total_rows = 0
        for family, items in analysis.items():
            if not isinstance(items, list):
                continue
            for rec in items:
                if isinstance(rec, dict):
                    app.append_record({"family": family, **rec})
                    total_rows += 1

        # Close appender if present
        closer = getattr(app, "close", None)
        if callable(closer):
            closer()

        # Mirror the 'wired={...}' style summary
        wired_pairs = ", ".join([f"{k}:{v}" for k, v in sorted(fam_counts.items())])
        log(f"Augment manifest: wired={{" + wired_pairs + f"}} total_rows={total_rows}")

        # Chunk & checksums (LOCAL)
        kind = str(conf.get("transport", {}).get("kind", "chunked")).lower()
        if kind == "chunked":
            parts = _write_parts_from_jsonl(
                jsonl_path=out_bundle,
                parts_dir=artifact_root,
                part_stem=cfg.transport.part_stem,
                part_ext=cfg.transport.part_ext,
                split_bytes=cfg.transport.split_bytes,
                parts_per_dir=cfg.transport.parts_per_dir,
            )
            log(f"chunk(local): wrote {len(parts)} parts")
            _ = _write_sha256sums_for_parts(
                parts_dir=artifact_root,
                part_stem=cfg.transport.part_stem,
                part_ext=cfg.transport.part_ext,
                sums_path=out_sums,
            )
            log(f"chunk report (local): {{'kind': 'local', 'decision': 'chunked', 'parts': {len(parts)}, 'split_bytes': {cfg.transport.split_bytes}}}")
            if not cfg.transport.preserve_monolith and out_bundle.exists():
                out_bundle.unlink(missing_ok=True)
        else:
            if out_bundle.exists():
                dg = sha256(out_bundle.read_bytes()).hexdigest()
                out_sums.write_text(f"{dg}  {out_bundle.name}\n", encoding="utf-8")
            log("chunk(local): decision=monolith")

        # (Optional) Analysis sidecars
        if bool(conf.get("publish_analysis", False)):
            log(f"publish_analysis (emitter gate): enabled  emitter=set")
            try:
                emitter = None
                try:
                    emitter = _load_analysis_emitter(repo_root)
                except TypeError:
                    emitter = _load_analysis_emitter()  # tolerate older signature
                if emitter and hasattr(emitter, "run"):
                    src = artifact_root  # emitter probes here for parts/index/jsonl
                    log(f"[analysis] source manifest dir: {src}")
                    target = artifact_root / "analysis"
                    log(f"[analysis] target analysis dir: {target}")
                    try:
                        emitter.run(project_root=repo_root, out_root=artifact_root, config=conf)
                    except TypeError:
                        emitter.run()
                else:
                    log("[analysis] emitter missing or has no 'run' method")
            except Exception:
                log("[analysis] ERROR during emission")
                traceback.print_exc()
        else:
            log("publish_analysis (emitter gate): disabled")

        # Final listing (helps sanity check)
        tree_paths = []
        for p in sorted(artifact_root.rglob("*")):
            if p.is_file():
                tree_paths.append(str(p.relative_to(artifact_root)))
        shown = 200
        if tree_paths:
            log(f"final tree under {artifact_root} (showing up to {shown} files; total={len(tree_paths)})")
            for i, rel in enumerate(tree_paths[:shown], 1):
                print(f"  {i:>3}. {rel}")
            if len(tree_paths) > shown:
                print(f"  ... (+{len(tree_paths)-shown} more)")
        else:
            log(f"final tree under {artifact_root}: (empty)")

        log("DONE")

    except Exception as e:
        log(f"FATAL: {e.__class__.__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()






