# File: v2/backend/core/prompt_pipeline/executor/providers.py
""" Generic providers for executor-stage capabilities.

This module must remain domain-agnostic. It turns DB/introspection records into
generic pipeline "items" the engine can pass to domain adapters via Spine.

Capabilities implemented here:
  - retriever.enrich.v1 -> enrich_v1
  - results.unpack.v1   -> unpack_results_v1
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
import fnmatch
import os


# ----------------------------- helpers ---------------------------------


@dataclass
class _Item:
    id: str
    path: Optional[str] = None
    relpath: Optional[str] = None
    signature: Optional[str] = None
    lang: Optional[str] = None
    context: Optional[Dict[str, Any]] = None


def _as_posix(p: Optional[str]) -> Optional[str]:
    if not p:
        return p
    try:
        return str(Path(p).as_posix())
    except Exception:
        return p


def _rel(root: str, path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()).as_posix())
    except Exception:
        # If path is outside root or cannot be relativized, just return POSIX-ish
        return _as_posix(path)


def _is_excluded(relpath: Optional[str], globs: Sequence[str]) -> bool:
    if not relpath or not globs:
        return False
    for g in globs:
        if not g:
            continue
        if fnmatch.fnmatchcase(relpath, g):
            return True
    return False


# ----------------------------- capabilities -------------------------------


def enrich_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """Build generic items from introspection records.

    Payload (task.payload + kwargs):
    {
      "root": "",
      "project_root": "",        # optional; defaults to CWD
      "records": [ {...}, ... ], # rows from introspect.fetch
      "exclude_globs": ["output/**", ...],
      "segment_excludes": ["**/__pycache__/**", ...]  # alias of exclude_globs
    }

    Returns:
    {
      "items": [
        {
          "id": "",
          "path": "",
          "relpath": "",
          "signature": "",
          "lang": "",
          "context": { ... }  # passthrough extras
        },
        ...
      ]
    }
    """
    payload: Dict[str, Any] = (getattr(task_like, "payload", None) or task_like or {})
    payload.update(kwargs or {})

    root = payload.get("root") or payload.get("project_root") or os.getcwd()
    records: List[Dict[str, Any]] = list(payload.get("records") or [])
    exclude_globs: List[str] = list(payload.get("exclude_globs") or payload.get("segment_excludes") or [])

    items: List[Dict[str, Any]] = []
    for r in records:
        rid = str(r.get("id") or r.get("relpath") or r.get("path") or "")
        # Crucial fix: accept DB row shapes {'file': '...'} or {'filepath': '...'}
        raw_path = r.get("path") or r.get("file") or r.get("filepath")
        path = _as_posix(raw_path)
        relpath = r.get("relpath") or _rel(str(root), path)

        if _is_excluded(relpath, exclude_globs):
            continue

        item = _Item(
            id=rid,
            path=path,
            relpath=relpath,
            signature=r.get("signature"),
            lang=r.get("lang") or r.get("language"),
            context=r.get("context") or {},
        )

        items.append(
            {
                "id": item.id,
                "path": _as_posix(item.path),
                "relpath": item.relpath,
                "signature": item.signature,
                "lang": item.lang,
                "context": item.context,
            }
        )

    result: Dict[str, Any] = {"items": items}
    if not items:
        result["warning"] = "No valid targets found for this run."
    return result


def unpack_results_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """Domain-agnostic 'unpack' that normalizes various result wrappers into {"items":[...]}.

    Payload accepts any of:
      - {"items":[...]}
      - {"results":[...]}
      - {"result":{"items":[...]}}
      - {"choices":[...]}  # raw LLM envelope (best-effort fallback)

    Returns: {"items":[ ...dict... ]}
    """
    payload: Dict[str, Any] = (getattr(task_like, "payload", None) or task_like or {})
    payload.update(kwargs or {})

    def _coerce(obj: Any) -> List[Dict[str, Any]]:
        if obj is None:
            return []
        if isinstance(obj, list):
            return [r for r in obj if isinstance(r, dict)]
        if isinstance(obj, dict):
            if isinstance(obj.get("items"), list):
                return [r for r in obj["items"] if isinstance(r, dict)]
            if isinstance(obj.get("results"), list):
                return [r for r in obj["results"] if isinstance(r, dict)]
            if isinstance(obj.get("result"), dict):
                return _coerce(obj["result"])
            if isinstance(obj.get("choices"), list) and obj["choices"]:
                # very light OpenAI-like unwrap
                first = obj["choices"][0]
                if isinstance(first, dict) and isinstance(first.get("message", {}).get("content"), str):
                    import json as _json
                    from v2.backend.core.prompt_pipeline.llm.response_parser import parse_json_response  # type: ignore

                    parsed = parse_json_response(first["message"]["content"])
                    return _coerce(parsed)
        return []

    items = _coerce(payload)
    return {"items": items}







