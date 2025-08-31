from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from packager.emitters.registry import get_reducer, zero_summary_for
from packager.core.writer import write_json_atomic


def emit_analysis_sidecars(
    *,
    manifest_iter: Iterable[Dict[str, Any]],
    gate: List[str],
    filenames: Mapping[str, str],
    emission_modes: Mapping[str, str],
    out_dir: Path,
    strategy: str = "backfill",
    synth_empty: bool = True,
    forbid_raw_secrets: bool = True,
) -> Dict[str, Any]:
    """
    Aggregate rows by family and write sidecar summaries for gated families.

    Returns an index dict:
      {
        "families": {
          "<family>": {"count": N, "path": "analysis/<file>", "mode": "both"|"manifest"|"none"}
        },
        "strategy": "backfill",
      }
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    gate_set = set(gate)
    counts: Dict[str, int] = {fam: 0 for fam in gate}
    buckets: Dict[str, List[Dict[str, Any]]] = {fam: [] for fam in gate}

    # Stream rows and bucket for gated families only
    for row in manifest_iter:
        fam = str(row.get("family", "")).strip()
        if fam not in gate_set:
            continue
        counts[fam] += 1
        buckets[fam].append(row)

    # Write per-family summaries
    index: Dict[str, Any] = {"families": {}, "strategy": strategy}

    for fam in gate:
        mode = (emission_modes.get(fam) or "manifest").lower()
        should_write = mode == "both"
        fam_file = filenames.get(fam)
        fam_items = buckets.get(fam, [])
        fam_count = counts.get(fam, 0)

        if fam_count == 0:
            if strategy == "enforce":
                raise RuntimeError(f"Emitter(enforce): family '{fam}' has zero rows in manifest")
            if not synth_empty or not should_write:
                index["families"][fam] = {"count": 0, "mode": mode, "path": None}
                continue
            summary = zero_summary_for(fam)
        else:
            reducer = get_reducer(fam)
            if fam == "secrets" and forbid_raw_secrets:
                summary = {
                    "family": fam,
                    "stats": {"count": fam_count},
                    "items": [],
                    "note": "raw secret payloads are not persisted by policy",
                }
            else:
                summary = reducer(fam_items)

        if should_write and fam_file:
            target = out_dir / fam_file
            write_json_atomic(target, summary)
            index["families"][fam] = {"count": fam_count, "mode": mode, "path": target.name}
        else:
            index["families"][fam] = {"count": fam_count, "mode": mode, "path": None}

    return index
