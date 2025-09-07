# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/analysis_emitter.py
"""
Analysis emitter (design_manifest → analysis/* summaries)

This version is STRICTLY config-driven. It does not probe for alternate
manifest locations. The manifest root MUST be provided by config:
  config.manifest_paths.root_dir

Config keys consumed
--------------------
- source_root: str
- manifest_paths:
    root_dir: "design_manifest"
    analysis_subdir: "analysis"
    analysis_index_filename: "_index.json"
- transport:
    part_stem: "design_manifest"
    part_ext: ".txt"
    monolith_ext: ".jsonl"
- manifest.reader:
    prefer_parts_index: true
- controls:
    forbid_raw_secrets: true
- metadata_emission: { family: "both"|"manifest-only"|"none" }   # optional
- controls.default_metadata_mode: "both"                          # optional
- controls.metadata_overrides: { family: "manifest"|"none"|... }  # optional
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping as TMapping, MutableMapping, Optional, Tuple

# Local imports
try:
    # Preferred location
    from .manifest.reader import ManifestReader  # type: ignore
except Exception:  # pragma: no cover
    # Fallback relative import for alternate execution layouts
    from packager.manifest.reader import ManifestReader  # type: ignore


# ──────────────────────────────────────────────────────────────────────────────
# Small utilities
# ──────────────────────────────────────────────────────────────────────────────

def _pfx(msg: str) -> str:
    return f"[analysis_emitter] {msg}"

def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON with a temp file then rename for atomicity on most OSes."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Config-safe getter: supports dict-like and attribute-like containers."""
    try:
        if isinstance(obj, Mapping):
            return obj.get(key, default)
        return getattr(obj, key, default)
    except Exception:
        return default

def _is_verbose(cfg) -> bool:
    controls = _get(cfg, "controls", {}) or {}
    # default True to restore legacy chatty behavior
    return bool(_get(controls, "verbose_analysis_logging", True))

def _summary_filename_for_family(cfg, family: str) -> str:
    # Prefer explicit mapping in YAML (analysis_filenames)
    fn_map = _get(cfg, "analysis_filenames", {}) or {}
    name = _get(fn_map, family, None)
    if name:
        return str(name)
    # Fallback to a conventional summary filename if unmapped
    return f"{family}.summary.json"



# ──────────────────────────────────────────────────────────────────────────────
# Config-driven path + naming resolution (STRICT)
# ──────────────────────────────────────────────────────────────────────────────

def _manifest_dir_from_cfg(cfg) -> Path:
    """
    Resolve the *single* manifest directory from config only.
    - Uses cfg.manifest_paths.root_dir.
    - If relative, it is resolved under cfg.source_root.
    - No candidates, no probing. If not present or empty → error in caller.
    """
    sr = Path(_get(cfg, "source_root", ".")).resolve()
    mp = _get(cfg, "manifest_paths", {}) or {}
    root_dir = _get(mp, "root_dir", None)
    if not root_dir:
        raise RuntimeError("config.manifest_paths.root_dir is required but missing")

    p = Path(root_dir)
    path = p if p.is_absolute() else (sr / p)
    return path.resolve()


def _resolve_manifest_dir(cfg) -> Tuple[Path, Dict[str, Any]]:
    """
    Strict config-only resolution:
      - manifest_dir = cfg.manifest_paths.root_dir (absolute or relative to cfg.source_root)
      - Validate that it contains either parts (*.txt) or monolith (*.jsonl), as per transport.
    """
    diagnostics: Dict[str, Any] = {"selected": None, "has_parts": False, "has_jsonl": False}

    transport = _get(cfg, "transport", {}) or {}
    part_stem = str(_get(transport, "part_stem", "design_manifest"))
    part_ext = str(_get(transport, "part_ext", ".txt"))
    monolith_ext = str(_get(transport, "monolith_ext", ".jsonl"))

    manifest_dir = _manifest_dir_from_cfg(cfg)
    diagnostics["selected"] = str(manifest_dir)

    has_parts = any(manifest_dir.glob(f"{part_stem}_*{part_ext}"))
    has_jsonl = (manifest_dir / f"{part_stem}{monolith_ext}").exists()
    diagnostics["has_parts"] = has_parts
    diagnostics["has_jsonl"] = has_jsonl

    if not (has_parts or has_jsonl):
        raise RuntimeError(
            f"No manifest found at {manifest_dir} "
            f"(expected {part_stem}_*{part_ext} or {part_stem}{monolith_ext}). "
            "Fix config.manifest_paths.root_dir or produce the manifest."
        )

    return manifest_dir, diagnostics


# ──────────────────────────────────────────────────────────────────────────────
# Emission policy (modes) and file naming
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_modes_from_cfg(cfg) -> TMapping[str, str]:
    """
    Determine emission mode per family.
    Priority (last-wins):
      1) controls.default_metadata_mode (applies to all)
      2) metadata_emission mapping
      3) controls.metadata_overrides mapping
    """
    controls = _get(cfg, "controls", {}) or {}
    default_mode = _get(controls, "default_metadata_mode", "both")
    modes: Dict[str, str] = defaultdict(lambda: str(default_mode))

    # Explicit family mapping
    me = _get(cfg, "metadata_emission", None)
    if isinstance(me, Mapping):
        for fam, mode in me.items():
            modes[str(fam)] = str(mode)
    elif hasattr(me, "__dict__"):
        for fam, mode in vars(me).items():
            modes[str(fam)] = str(mode)

    # Overrides
    overrides = _get(controls, "metadata_overrides", None)
    if isinstance(overrides, Mapping):
        for fam, mode in overrides.items():
            modes[str(fam)] = str(mode)
    elif hasattr(overrides, "__dict__"):
        for fam, mode in vars(overrides).items():
            modes[str(fam)] = str(mode)

    return modes


def _filename_for_family(family: str) -> str:
    """Conservative, deterministic filename for a family."""
    # Keep it simple and stable. If you want configurability here later,
    # add manifest_paths.family_filename_template to packager.yml
    return f"{family}.json"


# ──────────────────────────────────────────────────────────────────────────────
# Reduction / sidecar writing
# ──────────────────────────────────────────────────────────────────────────────

def _sanitize_item(it: Mapping[str, Any], forbid_raw_secrets: bool) -> Mapping[str, Any]:
    """
    Remove obviously sensitive fields. Keep common metadata fields.
    We assume 'payload' may contain verbose guts; drop it entirely by default.
    """
    safe: Dict[str, Any] = {}
    for k, v in it.items():
        k_l = str(k).lower()
        if k_l in {"payload", "raw"}:
            continue
        if forbid_raw_secrets and any(tok in k_l for tok in ("secret", "token", "password", "key")):
            continue
        # Avoid huge nested structures: only keep scalars and short dicts/lists
        if isinstance(v, (str, int, float, bool)) or v is None:
            safe[k] = v
        elif isinstance(v, Mapping):
            # Keep shallow copy of small maps (<= 8 keys)
            if len(v) <= 8:
                safe[k] = {kk: vv for kk, vv in v.items() if isinstance(vv, (str, int, float, bool)) or vv is None}
        elif isinstance(v, list):
            # Keep small lists of scalars (<= 16)
            scalars = [vv for vv in v if isinstance(vv, (str, int, float, bool)) or vv is None]
            if scalars and len(scalars) <= 16:
                safe[k] = scalars
    return safe


def _reduce_family(items: Iterable[Mapping[str, Any]], forbid_raw_secrets: bool) -> Dict[str, Any]:
    """
    Minimal reducer: count + sanitized sample of items.
    Deterministic ordering by 'name' or 'id' when present, else insertion order.
    """
    items_list = list(items)
    count = len(items_list)

    # Sort deterministically by a common key if present
    def _key(it: Mapping[str, Any]):
        if "name" in it:
            return ("name", str(it["name"]))
        if "id" in it:
            return ("id", str(it["id"]))
        if "path" in it:
            return ("path", str(it["path"]))
        return ("", "")

    items_list.sort(key=_key)

    # Take a bounded sample for sidecar to avoid huge dumps
    sample_max = 200  # can be made configurable later if needed
    sample = [_sanitize_item(it, forbid_raw_secrets) for it in items_list[:sample_max]]

    return {
        "count": count,
        "sample_size": len(sample),
        "items": sample,
        "no_data": count == 0,
    }


def _emit_analysis_sidecars(
    *,
    cfg,
    manifest_iter: Iterator[Mapping[str, Any]],
    out_dir: Path,
    emission_modes: TMapping[str, str],
    forbid_raw_secrets: bool,
) -> Dict[str, Any]:
    """
    Write per-family *summary* JSON sidecars only.
    - Filenames come from config.analysis_filenames[family] when present.
    - No low-level 'items' dumps; we only emit legacy summary shape.
    """
    verbose = _is_verbose(cfg)
    families: MutableMapping[str, List[Mapping[str, Any]]] = defaultdict(list)
    total = 0

    for item in manifest_iter:
        fam = str(item.get("family", "unknown"))
        families[fam].append(item)
        total += 1

    controls = _get(cfg, "controls", {}) or {}
    synth_empty = bool(_get(controls, "synthesize_empty_summaries", True))

    index: Dict[str, Any] = {
        "total_items": total,
        "families": {},
        "strategy": "summary-only",
    }

    for fam, items in sorted(families.items(), key=lambda kv: kv[0]):
        mode = emission_modes.get(fam, emission_modes.get("*", "both"))

        # Compose legacy-style summary payload
        summary_doc = {
            "no_data": len(items) == 0,
            "top": [],                       # keep empty for now (legacy tiny summaries)
            "totals": {"count": len(items)}, # ensure summaries aren’t empty
        }

        # Decide filename from YAML map
        fname = _summary_filename_for_family(cfg, fam)

        wrote = False
        # Respect modes + empty synthesis
        if mode in ("both", "manifest-only"):
            # Write a sidecar only if allowed by mode AND (has data or synthesize empty is true)
            if mode == "both" and (synth_empty or len(items) > 0):
                write_json_atomic(out_dir / fname, summary_doc)
                wrote = True

            # Index entry (path + count retained for quick stats)
            index["families"][fam] = {
                "count": len(items),
                "mode": mode,
                "path": fname,
            }

            if verbose:
                if wrote:
                    print(f"[packager] analysis: {fam} -> {fname} (count={len(items)}, mode={mode})")
                else:
                    print(f"[packager] analysis: {fam} -> (no sidecar; mode={mode}, count={len(items)})")
        else:
            # 'none' → do not write, still index presence with no path
            index["families"][fam] = {
                "count": len(items),
                "mode": mode,
                "path": None,
            }
            if verbose:
                print(f"[packager] analysis: {fam} suppressed (mode=none, count={len(items)})")

    if verbose:
        total_written = sum(1 for v in index["families"].values() if v.get("path"))
        print(f"[packager] analysis: wrote {total_written} summary file(s)")

    return index



# ──────────────────────────────────────────────────────────────────────────────
# Public entrypoints
# ──────────────────────────────────────────────────────────────────────────────

def emit_all(cfg, repo_root: Optional[Path] = None, **_) -> Dict[str, Any]:
    """
    Main API used by executor/orchestrator.
    Back-compat: accepts an optional repo_root kwarg (ignored unless cfg.source_root is absent).
    Returns the written index (dict).
    """
    # Back-compat shim: if executor provided repo_root but cfg lacks source_root, adopt it.
    if repo_root is not None and not _get(cfg, "source_root", None):
        try:
            setattr(cfg, "source_root", str(Path(repo_root)))
        except Exception:
            pass

    manifest_dir, _diag = _resolve_manifest_dir(cfg)

    # Resolve output directory
    manifest_paths = _get(cfg, "manifest_paths", {}) or {}
    analysis_subdir = str(_get(manifest_paths, "analysis_subdir", "analysis"))
    out_dir = (manifest_dir / analysis_subdir).resolve()
    _ensure_dir(out_dir)

    verbose = _is_verbose(cfg)
    if verbose:
        print(f"[packager] analysis: writing summaries to {out_dir}")

    # Reader configuration
    manifest_block = _get(cfg, "manifest", {}) or {}
    reader_cfg = _get(manifest_block, "reader", {}) or {}
    transport = _get(cfg, "transport", {}) or {}

    part_stem = str(_get(transport, "part_stem", "design_manifest")) + "_"
    part_ext = str(_get(transport, "part_ext", ".txt"))
    prefer_parts_index = bool(_get(reader_cfg, "prefer_parts_index", True))

    reader = ManifestReader(
        manifest_dir=manifest_dir,
        part_stem=part_stem,
        part_ext=part_ext,
        prefer_parts_index=prefer_parts_index,
    )

    # Emission policy and secrets handling
    modes = dict(_resolve_modes_from_cfg(cfg))
    controls = _get(cfg, "controls", {}) or {}
    forbid_raw_secrets = bool(_get(controls, "forbid_raw_secrets", True))

    # Emit sidecars
    index = _emit_analysis_sidecars(
        cfg=cfg,
        manifest_iter=reader.iter_manifest(),
        out_dir=out_dir,
        emission_modes=modes,
        forbid_raw_secrets=forbid_raw_secrets,
    )

    # Write index
    analysis_index_filename = str(_get(manifest_paths, "analysis_index_filename", "_index.json"))
    write_json_atomic(out_dir / analysis_index_filename, index)
    return index


def emit_from_config() -> Dict[str, Any]:
    """
    Convenience CLI entrypoint.
    Relies on the standard config builder in execute.config.
    """
    try:
        # Standard builder path
        from ..execute.config import build_cfg  # type: ignore
    except Exception:
        try:
            # Alternate import path depending on how PYTHONPATH is set
            from packager.execute.config import build_cfg  # type: ignore
        except Exception as e:
            raise RuntimeError("Cannot import build_cfg from execute.config; run via executor or ensure package layout.") from e

    cfg = build_cfg()
    return emit_all(cfg)


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
