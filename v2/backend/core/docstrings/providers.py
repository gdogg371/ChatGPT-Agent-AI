# File: v2/backend/core/docstrings/providers.py
"""
Docstrings-domain capabilities.

This module contains ONLY docstring-specific logic and is invoked exclusively
via the Spine capability registry. It must NOT import from `prompt_pipeline`
(executor/engine/etc). Keep it self-contained inside the `docstrings` package.

Capabilities implemented here:
  - context.build.v1
  - context.read_source_window.v1
  - prompts.build.v1
  - sanitize.v1
  - verify.v1
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .context import (
    build_context_for_items,
    read_source_window as _read_source_window,
)


# ------------------------------ payload utils ------------------------------


def _norm_root(payload: Dict[str, Any]) -> Path:
    root = payload.get("project_root") or payload.get("root") or "."
    return Path(str(root)).expanduser().resolve()


def _coerce_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    # Accept "items" or "records" for flexibility
    if isinstance(payload.get("items"), list):
        return [x for x in payload["items"] if isinstance(x, dict)]
    if isinstance(payload.get("records"), list):
        return [x for x in payload["records"] if isinstance(x, dict)]
    return []


def _ensure_id(idx: int, it: Dict[str, Any]) -> str:
    return str(
        it.get("id")
        or it.get("unique_key_hash")
        or it.get("hash")
        or it.get("relpath")
        or it.get("path")
        or it.get("file")
        or f"item-{idx}"
    )


# -------------------------- capability: context.build.v1 --------------------


def context_build_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Build docstring-friendly context for each item.

    Payload:
      {
        "root" | "project_root": str,
        "items" | "records": [ { "relpath"|"path"|"file": str,
                                 "target_lineno"|"line"|"lineno": int,
                                 "context": {...} } ],
        "options": { "trailing_after_node": int, "module_max_lines": int, "fallback_node_body_len": int }
      }

    Returns:
      { "items": [ same items, with item["context"] updated to include {"signature","context_code"} ] }
    """
    payload: Dict[str, Any] = (getattr(task_like, "payload", None) or task_like or {})
    payload.update(kwargs or {})
    project_root = _norm_root(payload)
    items = _coerce_items(payload)
    out = build_context_for_items(project_root=project_root, items=items, options=payload.get("options"))
    return {"items": out}


# --------------- capability: context.read_source_window.v1 ------------------


def context_read_source_window_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Read a raw source window around a given line number.

    Payload:
      { "root" | "project_root": str, "path"|"relpath"|"file": str, "line": int,
        "before": int=20, "after": int=20 }

    Returns:
      { "window": { "code": str, "start_line": int, "end_line": int, "path": str } }
    """
    payload: Dict[str, Any] = (getattr(task_like, "payload", None) or task_like or {})
    payload.update(kwargs or {})
    project_root = _norm_root(payload)
    rel_or_path = payload.get("relpath") or payload.get("path") or payload.get("file") or ""
    line = int(payload.get("line") or payload.get("lineno") or payload.get("target_lineno") or 0)
    before = int(payload.get("before", 20))
    after = int(payload.get("after", 20))
    win = _read_source_window(
        project_root=project_root,
        relpath_or_path=rel_or_path,
        center_lineno=line,
        before=before,
        after=after,
    )
    return {"window": win}


# --------------------------- capability: prompts.build.v1 -------------------


def _build_system_preamble() -> str:
    return (
        "You are a meticulous Python docstring writer.\n"
        "- Follow PEP 257 conventions (one-line summary, blank line, extended description).\n"
        "- Keep summaries short, imperative, and clear.\n"
        "- Include Args/Returns/Raises only when present or inferable.\n"
        "- Do not change code; only propose docstrings.\n"
    )


def _item_to_user_prompt(it: Dict[str, Any]) -> str:
    rel = it.get("relpath") or it.get("path") or it.get("file") or "<unknown>"
    sig = (it.get("context") or {}).get("signature") or it.get("signature") or ""
    ctx = (it.get("context") or {}).get("context_code") or ""
    line = it.get("target_lineno") or it.get("line") or it.get("lineno") or ""
    header = f"Target: {rel} @ line {line}\n"
    if sig:
        header += f"Signature: {sig}\n"
    body = "Code context:\n" + ("-" * 60) + "\n" + ctx + ("\n" + "-" * 60 if ctx else "")
    instruction = (
        "\n\nTask: Write a high-quality docstring for the target symbol/module. "
        "Return JSON with fields: {\"id\",\"relpath\",\"docstring\"}."
    )
    return header + "\n" + body + instruction


def build_prompts_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Build LLM-ready prompts (system + batch of user prompts).

    Payload:
      { "items" | "records": [ {... item dicts ...} ] }

    Returns:
      {
        "messages": [
          {"role":"system","content": "..."}
        ],
        "batch": [
          {"id": "...", "role":"user","content":"..."},
          ...
        ],
        "ids": ["...", "..."]         # convenience list of ids in batch order
      }
    """
    payload: Dict[str, Any] = (getattr(task_like, "payload", None) or task_like or {})
    payload.update(kwargs or {})
    items = _coerce_items(payload)

    messages: List[Dict[str, str]] = [{"role": "system", "content": _build_system_preamble()}]
    batch: List[Dict[str, str]] = []
    ids: List[str] = []

    for idx, it in enumerate(items):
        item_id = _ensure_id(idx, it)
        ids.append(item_id)
        batch.append(
            {
                "id": item_id,
                "role": "user",
                "content": _item_to_user_prompt(it),
            }
        )

    return {
        "messages": messages,
        "batch": batch,
        "ids": ids,
    }


# -------------------------- capability: sanitize.v1 -------------------------


def sanitize_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Sanitize/normalize raw docstrings returned by the LLM.

    This is intentionally minimal; richer formatting rules can be added here
    (e.g., trimming whitespace, ensuring trailing newline).
    """
    payload: Dict[str, Any] = (getattr(task_like, "payload", None) or task_like or {})
    payload.update(kwargs or {})

    items: List[Dict[str, Any]] = []
    for it in _coerce_items(payload):
        ds = (it.get("docstring") or "").rstrip()
        if ds and not ds.endswith("\n"):
            ds += "\n"
        items.append({**it, "docstring": ds})
    return {"items": items}


# --------------------------- capability: verify.v1 --------------------------


def verify_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Lightweight verification for docstring outputs.
    Flags items with missing/too-short docstrings.
    """
    payload: Dict[str, Any] = (getattr(task_like, "payload", None) or task_like or {})
    payload.update(kwargs or {})

    items_in = _coerce_items(payload)
    results: List[Dict[str, Any]] = []
    for it in items_in:
        ds = (it.get("docstring") or "").strip()
        ok = bool(ds) and len(ds.splitlines()[0]) >= 8  # minimal 1-line summary length
        results.append({**it, "verify_ok": ok, "verify_reason": None if ok else "Too short or missing"})
    return {"items": results}

