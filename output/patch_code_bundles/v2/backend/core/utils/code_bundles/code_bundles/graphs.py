# File: v2/backend/core/utils/code_bundles/code_bundles/graphs.py
"""
Graph helpers for dependency edges.

We operate on simple edge dicts shaped by contracts.build_graph_edge:
  {
    "kind": "graph.edge",
    "src_path": "<repo-rel posix path>",
    "dst_module": "<dotted module>",
    "edge_type": "import" | "from"
  }

Functions:
- coalesce_edges: deduplicate edges (src_path, dst_module, edge_type).
"""

from __future__ import annotations

from typing import Dict, Iterable, Iterator, List, Tuple


def _edge_key(e: Dict) -> Tuple[str, str, str]:
    return (
        str(e.get("src_path") or ""),
        str(e.get("dst_module") or ""),
        str(e.get("edge_type") or "import"),
    )


def coalesce_edges(edges: Iterable[Dict]) -> List[Dict]:
    """
    Deduplicate edges by (src_path, dst_module, edge_type).
    Keeps the first occurrence.
    """
    seen = set()
    out: List[Dict] = []
    for e in edges:
        k = _edge_key(e)
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out
