# v2/backend/core/utils/code_bundles/code_bundles/graphs.py

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple


def _edge_key(e: Dict) -> Tuple:
    # Normalize edge identity; prefer src/dst path, else module if present
    rt = e.get("record_type", "")
    src = e.get("src_path") or e.get("src") or ""
    dst = e.get("dst_path") or e.get("dst") or e.get("dst_module") or ""
    kind = e.get("kind") or rt
    return (kind, src, dst)


def coalesce_edges(edges: Iterable[Dict]) -> List[Dict]:
    """
    Deduplicate edges while preserving first-seen order.
    Works with both import edges and any other edge forms that include
    src_path/dst_path or dst_module.
    """
    out: List[Dict] = []
    seen = set()
    for e in edges or []:
        k = _edge_key(e)
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out
