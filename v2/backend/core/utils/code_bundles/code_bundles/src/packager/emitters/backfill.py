# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/emitters/backfill.py
"""
Analysis backfill emitter.

Purpose
-------
Aggregate raw manifest records (already normalized by the ManifestReader)
into compact analysis sidecar summaries (one JSON per family) and an
index describing what was emitted.

Key properties
--------------
- Stdlib-only; deterministic/stable output (stable sorts, explicit rounding).
- Family canonicalization is enforced here as a safety net.
- Honors gating (only emit for specific families).
- Honors emission modes per family (e.g., "manifest-only" to avoid writing).
- Avoids misleading files: when a family has zero items, emits a tiny zero
  summary (or `no_data` in family-specific reducers) rather than fake stats.
- Optional policy to avoid persisting raw secret payloads.
- **V1 intake**: consumes only rows where schema == "scanner.record.v1".
  Allows select meta record_types for io_core (manifest_header, bundle_summary).

Public API
----------
emit_analysis_sidecars(
    *,
    manifest_iter: Iterable[dict],
    gate: list[str],
    filenames: Mapping[str, str],
    emission_modes: Mapping[str, str],
    out_dir: Path,
    forbid_raw_secrets: bool = True,
) -> dict

Returns an index dict:
{
  "strategy": "backfill",
  "families": {
     "<family>": {"count": <int>, "mode": "<mode>", "path": "<filename or null>"},
     ...
  }
}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple
from collections import defaultdict

from v2.backend.core.utils.code_bundles.code_bundles.src.packager.emitters.registry import (
    get_reducer,
    zero_summary_for,
    canonicalize_family,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.core.writer import write_json_atomic


def _pfx(msg: str) -> str:
    return f"[analysis] {msg}"


def _norm_mode(mode: str | None) -> str:
    """
    Normalize an emission mode to one of:
      - "both"            → compute + write file
      - "manifest-only"   → compute only, don't write file
    Accepts aliases: {"write","file","analysis"} → "both"
                     {"manifest"} → "manifest-only"
    """
    m = (mode or "both").strip().lower()
    if m in {"both", "write", "file", "analysis"}:
        return "both"
    if m in {"manifest-only", "manifest"}:
        return "manifest-only"
    return "both"


def _should_write(mode: str) -> bool:
    return _norm_mode(mode) == "both"


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


# meta record_types we still accept (routed to io_core)
_META_TO_IO_CORE = {"manifest_header", "bundle_summary"}


def emit_analysis_sidecars(
    *,
    manifest_iter: Iterable[Dict[str, Any]],
    gate: List[str],
    filenames: Mapping[str, str],
    emission_modes: Mapping[str, str],
    out_dir: Path,
    forbid_raw_secrets: bool = True,
) -> Dict[str, Any]:
    """
    Build and (optionally) write analysis summaries for gated families.

    Parameters
    ----------
    manifest_iter : iterable of dict
        Normalized manifest records. For inclusion:
          - schema == "scanner.record.v1" (primary), OR
          - record_type in {"manifest_header","bundle_summary"} (routed to io_core).
    gate : list[str]
        Families to consider for emission. If empty, all seen families are considered.
    filenames : mapping
        Map of family → filename (e.g., {"quality": "quality.complexity.summary.json"}).
        If missing for a family, a default "<family>.summary.json" is used.
    emission_modes : mapping
        Map of family → mode ("both" or "manifest-only"). Missing → "both".
    out_dir : Path
        Destination directory for analysis files. Will be created if needed.
    forbid_raw_secrets : bool
        When True, secrets family will not persist raw payloads; instead a minimal
        summary is written (count only).

    Returns
    -------
    dict
        An index suitable for saving as analysis/_index.json (caller decides).
    """
    out_dir = Path(out_dir)
    _ensure_dir(out_dir)

    # Normalize gate to canonical families
    gate_set = {canonicalize_family(g) for g in (gate or []) if g}
    gate_enabled = bool(gate_set)

    # 1) Bucket items by canonical family with strict v1 intake (plus io_core meta)
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    total_seen = 0
    for obj in manifest_iter:
        if not isinstance(obj, dict):
            continue

        schema = obj.get("schema")
        record_type = obj.get("record_type")

        # Route allowed meta to io_core; otherwise require v1 schema.
        if schema == "scanner.record.v1":
            fam_source = obj.get("kind") or obj.get("family") or obj.get("type")
            fam = canonicalize_family(str(fam_source or ""))
        elif isinstance(record_type, str) and record_type in _META_TO_IO_CORE:
            fam = "io_core"
        else:
            # Not v1 and not an allowed meta record → skip
            continue

        if not fam:
            continue
        if gate_enabled and fam not in gate_set:
            # Skip families outside the gate
            continue

        # ──────────────────────────────────────────────────────────────────────
        # Per-family light adapter (for v1 rows) BEFORE bucketing
        # ──────────────────────────────────────────────────────────────────────
        item = obj
        if schema == "scanner.record.v1":
            # prefer 'payload', then 'data'
            payload = obj.get("payload") or obj.get("data") or {}
            item = dict(payload)  # shallow copy

            # envelope hints for reducers
            if isinstance(obj.get("path"), str):
                item.setdefault("file", obj["path"])
            # preserve original kind info where useful
            if obj.get("kind"):
                item.setdefault("kind", obj.get("kind"))

            # family-specific light coercions
            if fam == "ast_calls":
                # expected by reducer: 'name'/'call' for the callee
                callee = payload.get("callee")
                if isinstance(callee, str) and callee:
                    item.setdefault("name", callee)

            elif fam == "ast_imports":
                # expected: module name and edges list with 'to'
                mod = payload.get("module")
                edge = payload.get("edge") or {}
                if not isinstance(mod, str) and isinstance(edge, dict):
                    # try edge.to.name
                    to = edge.get("to") or {}
                    if isinstance(to, dict):
                        mod = to.get("name") or mod
                if isinstance(mod, str) and mod:
                    item.setdefault("module", mod)
                tgt = payload.get("to")
                if isinstance(tgt, str) and tgt:
                    item.setdefault("edges", [{"to": tgt}])

            elif fam == "ast_symbols":
                # expected: kind/name possibly nested under 'symbol'
                sym = payload.get("symbol") or {}
                if isinstance(sym, dict):
                    k = sym.get("kind")
                    n = sym.get("name")
                    if isinstance(k, str) and k:
                        item.setdefault("kind", k)
                    if isinstance(n, str) and n:
                        item.setdefault("name", n)

            elif fam == "docs":
                # expected: coverage numeric
                cov = payload.get("coverage")
                if cov is None:
                    cov = payload.get("doc_coverage")
                if cov is not None:
                    item.setdefault("coverage", cov)

            elif fam == "sql":
                # expected: 'statements' list of dicts
                if isinstance(payload.get("statement"), dict):
                    item.setdefault("statements", [payload["statement"]])

            elif fam == "deps":
                # pass-through: reducer already tolerates name/version in payload
                pass

            elif fam == "env":
                # pass-through: reducer expects 'used'/'declared' lists if present
                pass

            elif fam == "entrypoints":
                # pass-through: reducer uses kind/name/target if present
                pass

        elif isinstance(record_type, str) and record_type in _META_TO_IO_CORE:
            # io_core expects keys 'manifest_header' / 'bundle_summary'
            payload = obj.get("payload") or obj.get("data") or {}
            if record_type == "manifest_header":
                item = {"manifest_header": payload}
            elif record_type == "bundle_summary":
                item = {"bundle_summary": payload}
            else:
                item = obj  # fallback, though we shouldn't hit here

        # Keep adapted row shape; reducers handle specifics.
        buckets[fam].append(item)
        total_seen += 1

    # 2) Ensure that gated families appear in the index even if no items
    if gate_enabled:
        for fam in sorted(gate_set):
            buckets.setdefault(fam, [])

    # 3) Reduce per family and emit files per mode/filename policy
    index: Dict[str, Any] = {"strategy": "backfill", "families": {}}

    for fam in sorted(buckets.keys()):
        items = buckets[fam]
        fam_count = len(items)

        # Determine filename and mode
        fam_file = filenames.get(fam) if isinstance(filenames, Mapping) else None
        if not fam_file:
            # Default naming if not provided by cfg
            fam_file = f"{fam}.summary.json"
        mode = _norm_mode(emission_modes.get(fam) if isinstance(emission_modes, Mapping) else None)
        should_write = _should_write(mode)

        # Compute summary
        reducer = get_reducer(fam)
        if fam == "secrets" and forbid_raw_secrets:
            # Do not include any raw secret payloads; only counts.
            summary = {
                "family": "secrets",
                "stats": {"count": fam_count},
                "items": [],
                "note": "raw secret payloads are not persisted by policy",
            }
        else:
            if fam_count == 0:
                summary = zero_summary_for(fam)
            else:
                summary = reducer(items)

        # Write or not based on mode
        if should_write and fam_file:
            target = out_dir / fam_file
            write_json_atomic(target, summary)
            index["families"][fam] = {"count": fam_count, "mode": mode, "path": target.name}
            print(_pfx(f"emit[{fam}]: rows={fam_count} -> {target}"))
        else:
            index["families"][fam] = {"count": fam_count, "mode": mode, "path": None}
            print(_pfx(f"emit[{fam}]: rows={fam_count} mode={mode} (manifest-only/no filename)"))

    # 4) Summary line
    fams = index.get("families", {})
    emitted = sum(1 for v in fams.values() if v.get("path"))
    nonzero = sum(1 for v in fams.values() if v.get("count", 0) > 0)
    print(_pfx(f"wrote {emitted}/{len(fams)} families  (nonzero: {nonzero})  total_seen_records={total_seen}"))

    return index




