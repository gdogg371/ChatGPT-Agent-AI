# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/analysis_emitter.py
"""
Analysis emitter (design_manifest → analysis/* summaries)

Responsibilities
----------------
- Read normalized manifest items via ManifestReader.
- Reduce items per family and write stable, deterministic summaries into
  design_manifest/analysis/.
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
from typing import Any, Dict, List, Mapping, Optional

from packager.core.loader import load_packager_config  # uses config/packager.yml
from packager.manifest.reader import ManifestReader
from packager.emitters.backfill import emit_analysis_sidecars
from packager.core.writer import write_json_atomic


__all__ = ["emit_from_config", "emit_analysis"]


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _pfx(msg: str) -> str:
    return f"[analysis] {msg}"


def _analysis_dir_from_cfg(cfg) -> Path:
    """
    Resolve the target analysis directory for summaries.

    We follow the convention used elsewhere in the packager:
      <source_root>/design_manifest/analysis/
    """
    src_root = Path(getattr(cfg, "source_root", "."))
    return (src_root / "design_manifest" / "analysis").resolve()


def _manifest_dir_from_cfg(cfg) -> Path:
    """
    Resolve the manifest directory that contains the part files:
      <source_root>/design_manifest/
    """
    src_root = Path(getattr(cfg, "source_root", "."))
    return (src_root / "design_manifest").resolve()


def _default_filenames() -> Dict[str, str]:
    """
    Canonical family → filename mapping under analysis/.
    These match the filenames present in existing bundles.
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
    modes = {
        fam: "both" for fam in _default_filenames().keys()
    }
    # SBOM is usually produced elsewhere; avoid clobbering if present.
    modes["sbom"] = "manifest-only"
    return modes


def _gate_from_cfg(cfg) -> List[str]:
    """
    Determine which families to emit.
    If config provides explicit gate, use it; else, emit for all known families.
    """
    # Optional: cfg.analysis.gate: [families...]
    gate: Optional[List[str]] = None
    try:
        analysis = getattr(cfg, "analysis", None)
        if isinstance(analysis, dict):
            maybe_gate = analysis.get("gate")
            if isinstance(maybe_gate, list) and maybe_gate:
                gate = [str(x) for x in maybe_gate]
    except Exception:
        gate = None

    return gate or list(_default_filenames().keys())


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def emit_analysis(*, cfg=None) -> Dict[str, Any]:
    """
    Main entry point (programmatic): emit analysis summaries to the
    configured design_manifest/analysis directory based on the current
    design_manifest parts in <source_root>/design_manifest/.

    Returns the analysis index dict (suitable for writing to analysis/_index.json).
    """
    if cfg is None:
        cfg = load_packager_config()

    manifest_dir = _manifest_dir_from_cfg(cfg)
    out_dir = _analysis_dir_from_cfg(cfg)

    out_dir.mkdir(parents=True, exist_ok=True)

    print(_pfx(f"source manifest dir: {manifest_dir}"))
    print(_pfx(f"target analysis dir: {out_dir}"))

    reader = ManifestReader(
        manifest_dir=manifest_dir,
        part_stem="design_manifest_",
        part_ext=".txt",
        prefer_parts_index=True,
    )

    # Gate, filenames, and modes
    filenames = _default_filenames()
    modes = _default_modes()
    gate = _gate_from_cfg(cfg)

    # Build and (optionally) write sidecars
    index = emit_analysis_sidecars(
        manifest_iter=reader.iter_manifest(),
        gate=gate,
        filenames=filenames,
        emission_modes=modes,
        out_dir=out_dir,
        forbid_raw_secrets=True,
    )

    # Persist the index
    write_json_atomic(out_dir / "_index.json", index)

    fams = index.get("families", {})
    emitted = sum(1 for v in fams.values() if v.get("path"))
    total = len(fams)
    nonzero = sum(1 for v in fams.values() if v.get("count", 0) > 0)
    print(_pfx(f"wrote {emitted}/{total} families  (nonzero: {nonzero})"))
    return index


def emit_from_config() -> Dict[str, Any]:
    """
    Convenience wrapper to load config and emit analysis.
    """
    cfg = load_packager_config()
    return emit_analysis(cfg=cfg)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Minimal CLI: no args needed; relies on config/packager.yml
    try:
        idx = emit_from_config()
        print(_pfx(f"index written with {len(idx.get('families', {}))} families"))
    except Exception as e:
        # Keep CLI noise low but explicit
        import sys, traceback
        print(_pfx(f"ERROR: {e}"), file=sys.stderr)
        traceback.print_exc()
        raise



