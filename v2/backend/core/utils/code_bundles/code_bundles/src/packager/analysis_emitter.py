# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/analysis_emitter.py
"""
Analysis emitter (design_manifest → analysis/* summaries)

Responsibilities
----------------
- Read normalized manifest items via ManifestReader.
- Reduce items per family and write stable, deterministic summaries into
  <manifest_dir>/analysis/.
- Maintain an analysis/_index.json describing emitted families, counts,
  and target filenames.
- Avoid misleading outputs (family-specific reducers handle `no_data`
  cases; secrets never serialize raw payloads).

Key integrations
----------------
- Config:  packager.core.loader.load_packager_config  (reads config/packager.yml)
- Reader:  packager.manifest.reader.ManifestReader
- Reduce:  packager.emitters.backfill.emit_analysis_sidecars
- Writer:  packager.core.writer.write_json_atomic

This module is stdlib-only and makes no network calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

from packager.core.loader import load_packager_config  # uses config/packager.yml
from packager.manifest.reader import ManifestReader
from packager.emitters.backfill import emit_analysis_sidecars
from packager.core.writer import write_json_atomic


__all__ = ["emit_from_config", "emit_analysis", "emit_all"]


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _pfx(msg: str) -> str:
    return f"[analysis] {msg}"


def _candidate_manifest_dirs(cfg) -> List[Path]:
    """
    Return candidate directories that may contain manifest parts for this run.
    Priority:
      1) <source_root>/design_manifest
      2) <source_root>/output/design_manifest
      3) <source_root>/output/patch_code_bundles/published/design_manifest   (if present)
    """
    sr = Path(getattr(cfg, "source_root", ".")).resolve()
    cands = [
        sr / "design_manifest",
        sr / "output" / "design_manifest",
        sr / "output" / "patch_code_bundles" / "published" / "design_manifest",
    ]
    # Deduplicate while preserving order
    seen = set()
    uniq: List[Path] = []
    for p in cands:
        if p not in seen:
            uniq.append(p)
            seen.add(p)
    return uniq


def _has_parts(dirpath: Path, part_stem: str = "design_manifest_", part_ext: str = ".txt") -> bool:
    try:
        return any(dirpath.glob(f"{part_stem}*{part_ext}"))
    except Exception:
        return False


def _has_bundle_jsonl(dirpath: Path) -> bool:
    try:
        return (dirpath / "design_manifest.jsonl").exists()
    except Exception:
        return False


def _resolve_manifest_dir(cfg) -> Tuple[Path, Dict[str, Any]]:
    """
    Probe likely locations and choose the first directory that contains
    either parts or the jsonl bundle. Returns (dir, diagnostics).
    """
    diagnostics: Dict[str, Any] = {"candidates": [], "selected": None}
    for cand in _candidate_manifest_dirs(cfg):
        has_parts = _has_parts(cand)
        has_jsonl = _has_bundle_jsonl(cand)
        diagnostics["candidates"].append(
            {"path": str(cand), "has_parts": has_parts, "has_jsonl": has_jsonl}
        )
        if has_parts or has_jsonl:
            diagnostics["selected"] = str(cand)
            return cand, diagnostics
    # Fallback to first candidate
    first = _candidate_manifest_dirs(cfg)[0]
    diagnostics["selected"] = str(first)
    return first, diagnostics


def _default_filenames() -> Dict[str, str]:
    """
    Canonical family → filename mapping under analysis/.
    Matches filenames used by run_pack publishing.
    """
    return {
        # AST families
        "ast_calls": "ast.calls.summary.json",
        "ast_imports": "ast.imports.summary.json",
        "ast_symbols": "ast.symbols.summary.json",

        # Entrypoints / Env / Quality / SQL / Deps
        "entrypoints": "entrypoints.summary.json",
        "env": "env.summary.json",
        "quality": "quality.complexity.summary.json",
        "sql": "sql.index.summary.json",
        "deps": "deps.index.summary.json",

        # Code ownership / licensing / HTML / Git
        "codeowners": "codeowners.summary.json",
        "license": "license.summary.json",
        "html": "html.summary.json",
        "git": "git.info.summary.json",

        # JS index
        "js": "js.index.summary.json",

        # Assets
        "asset": "asset.summary.json",

        # Docs coverage / CS (client-side metrics bucket)
        "docs.coverage": "docs.coverage.summary.json",
        "cs": "cs.summary.json",

        # IO core (overall manifest summary)
        "io_core": "manifest.summary.json",

        # SBOM (CycloneDX) — treated specially (manifest-only)
        "sbom": "sbom.cyclonedx.json",

        # Secrets
        "secrets": "secrets.summary.json",
    }


def _default_modes() -> Dict[str, str]:
    """
    Family → emission mode.

    "both"          : compute + write analysis file
    "manifest-only" : compute only; do not overwrite the file (e.g., sbom)
    """
    modes = {fam: "both" for fam in _default_filenames().keys()}
    # SBOM is usually produced elsewhere; avoid clobbering if present.
    modes["sbom"] = "manifest-only"
    return modes


def _gate_from_cfg(cfg) -> List[str]:
    """
    Determine which families to emit.
    If config provides explicit gate, use it; else, emit all known families.
    """
    # Optional: cfg.analysis.gate: [families...]
    try:
        analysis = getattr(cfg, "analysis", None)
        if isinstance(analysis, dict):
            maybe_gate = analysis.get("gate")
            if isinstance(maybe_gate, list) and maybe_gate:
                return [str(x) for x in maybe_gate]
    except Exception:
        pass
    return list(_default_filenames().keys())


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def emit_analysis(*, cfg=None) -> Dict[str, Any]:
    """
    Emit analysis summaries into <manifest_dir>/analysis based on the
    current design_manifest parts.
    """
    if cfg is None:
        cfg = load_packager_config()

    # Choose the actual manifest dir for this run (repo-root or output/)
    manifest_dir, diag = _resolve_manifest_dir(cfg)

    # IMPORTANT: tie the analysis output directory to the selected manifest_dir
    out_dir = (manifest_dir / "analysis").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(_pfx(f"source manifest dir: {manifest_dir}"))
    print(_pfx(f"target analysis dir: {out_dir}"))
    # Helpful probe if counts unexpectedly zero
    try:
        print(_pfx(f"probe: {diag}"))
    except Exception:
        pass

    reader = ManifestReader(
        manifest_dir=manifest_dir,
        part_stem="design_manifest_",
        part_ext=".txt",
        prefer_parts_index=True,
    )

    filenames = _default_filenames()
    modes = _default_modes()
    gate = _gate_from_cfg(cfg)

    index = emit_analysis_sidecars(
        manifest_iter=reader.iter_manifest(),
        gate=gate,
        filenames=filenames,
        emission_modes=modes,
        out_dir=out_dir,
        forbid_raw_secrets=True,
    )

    write_json_atomic(out_dir / "_index.json", index)

    fams = index.get("families", {})
    emitted = sum(1 for v in fams.values() if v.get("path"))
    total = len(fams)
    nonzero = sum(1 for v in fams.values() if v.get("count", 0) > 0)
    print(_pfx(f"wrote {emitted}/{total} families  (nonzero: {nonzero})"))
    return index


def emit_from_config() -> Dict[str, Any]:
    """Convenience wrapper used by ad-hoc runners."""
    cfg = load_packager_config()
    return emit_analysis(cfg=cfg)


def emit_all(*, repo_root: Path | str, cfg) -> Dict[str, Any]:
    """
    Compatibility shim for manage_github.py

    run_pack dynamically imports:
        from src.packager.analysis_emitter import emit_all
    and invokes it like:
        _emit_analysis_sidecars(repo_root=Path(cfg.source_root).resolve(), cfg=cfg)

    We ignore `repo_root` (paths are resolved via cfg + autodetect), but we
    validate for sanity, then delegate to emit_analysis.
    """
    try:
        rr = Path(repo_root).resolve()
        sr = Path(getattr(cfg, "source_root", ".")).resolve()
        if rr != sr:
            print(_pfx(f"NOTE: repo_root {rr} differs from cfg.source_root {sr}; using autodetected manifest dir."))
    except Exception:
        pass
    return emit_analysis(cfg=cfg)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        idx = emit_from_config()
        print(_pfx(f"index written with {len(idx.get('families', {}))} families"))
    except Exception as e:
        import sys, traceback
        print(_pfx(f"ERROR: {e}"), file=sys.stderr)
        traceback.print_exc()
        raise

