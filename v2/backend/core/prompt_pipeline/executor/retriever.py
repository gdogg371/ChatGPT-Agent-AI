# File: v2/backend/core/prompt_pipeline/executor/retriever.py
"""
Generic retrieval helpers (domain-agnostic).

NOTE: The engine calls the Spine capability `retriever.enrich.v1` implemented
in `executor.providers:enrich_v1`. This module contains optional helpers for
future use and remains neutral.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Record:
    id: str
    path: Optional[str] = None
    relpath: Optional[str] = None
    signature: Optional[str] = None
    lang: Optional[str] = None
    context: Dict[str, Any] = None


def normalize_record(root: str, r: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert an introspection record into a neutral item dict.

    Keeps only portable fields:
      - id, path, relpath, signature, lang, context
    """
    rid = str(r.get("id") or r.get("relpath") or r.get("path") or "")
    path = r.get("path")
    relpath = r.get("relpath")
    if not relpath and path:
        try:
            relpath = str(Path(path).resolve().relative_to(Path(root).resolve()).as_posix())
        except Exception:
            relpath = str(Path(path).as_posix())

    return {
        "id": rid,
        "path": path,
        "relpath": relpath,
        "signature": r.get("signature"),
        "lang": r.get("lang") or r.get("language"),
        "context": r.get("context") or {},
    }

