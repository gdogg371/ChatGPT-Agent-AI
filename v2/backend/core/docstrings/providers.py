# File: v2/backend/core/docstrings/providers.py
from __future__ import annotations

"""
Docstrings adapter providers.

These functions are DOCSTRING-SPECIFIC but isolated in the adapter,
not in the core pipeline. They are tolerant of the generic item shape.

Capabilities wired here (see capabilities.yml):
  - prompts.build.v1          -> build_prompts_v1
  - sanitize.v1               -> sanitize_outputs_v1
  - verify.v1                 -> verify_batch_v1
"""

from typing import Any, Dict, List, Optional


# ----------------------------- helpers -----------------------------

def _to_messages_from_items(items: List[Dict[str, Any]], ask_spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Build a single chat batch (system+user) to request docstrings for N targets.
    This stays simple and robust; richer formatting can be added later.
    """
    sys = (
        "You are a precise code assistant. "
        "Write or improve Python docstrings following PEP 257 and Google-style sections. "
        "Return ONLY valid JSON with an 'items' array."
    )
    # Prepare a compact task list
    lines = [
        "For each target, produce an object: {\"id\": <id>, \"docstring\": <text>}.",
        "Targets:",
    ]
    for it in items[: max(1, len(items))]:
        tid = it.get("id")
        sig = (it.get("signature") or (it.get("context") or {}).get("signature") or "")
        name = (it.get("context") or {}).get("name") or ""
        # Optional context_code (if adapter/context.build provided it)
        snippet = (it.get("context") or {}).get("context_code") or ""
        entry = f"- id: {tid}"
        if sig:
            entry += f", signature: {sig}"
        if name:
            entry += f", name: {name}"
        if snippet:
            # Clip long snippet to keep message size reasonable
            snip = snippet
            if len(snip) > 1200:
                snip = snip[:1200] + "\n..."
            entry += f"\n  code:\n{snip}"
        lines.append(entry)

    user = "\n".join(lines)

    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ]


def _normalize_item_for_patch(it: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a parsed LLM item into a legacy-friendly shape:
      - Keep 'id'
      - Map 'docstring' or 'result' -> both 'docstring' and 'new_docstring'
      - Preserve path/relpath/signature when available
    """
    out: Dict[str, Any] = {"id": it.get("id")}
    val = it.get("docstring")
    if val is None:
        val = it.get("result")
    out["docstring"] = val
    out["new_docstring"] = val  # legacy-friendly
    # Carry context-through fields if present (harmless for generic patch engines)
    for k in ("path", "relpath", "signature", "lang"):
        if k in it:
            out[k] = it[k]
    # Optionally include adapter meta
    if isinstance(it.get("meta"), dict):
        out["meta"] = dict(it["meta"])
    return out


# ----------------------------- capabilities -----------------------------

def build_prompts_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Build a simple messages batch to request docstrings for the provided items.

    Payload:
      {
        "items": [ {"id":..., "signature":..., "context": {"context_code": "..."} }, ... ],
        "ask_spec": { ... }   # forwarded transparently
      }

    Returns:
      {
        "result": {
          "messages": [ {"role":"system","content":"..."}, {"role":"user","content":"..."} ],
          "ids": [ ... ],     # echo ids for convenience
          "batch": [ ... ]    # echo input items
        }
      }
    """
    payload: Dict[str, Any] = (getattr(task_like, "payload", None) or task_like or {})
    payload.update(kwargs or {})
    items = list(payload.get("items") or [])
    ask_spec = dict(payload.get("ask_spec") or {})

    if not items:
        # No targets — return empty messages to let the engine short-circuit.
        return {"result": {"messages": [], "ids": [], "batch": []}}

    msgs = _to_messages_from_items(items, ask_spec)
    ids = [i.get("id") for i in items]
    return {"result": {"messages": msgs, "ids": ids, "batch": items}}


def sanitize_outputs_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Accept parsed LLM items and normalize them into a stable shape for verification/patching.

    Payload:
      {
        "items": [ {"id":"...", "docstring":"..."} or {"id":"...", "result":"..."} , ... ],
        "prepared_batch": [ ... ]  # optional, for correlating context
      }

    Returns:
      {
        "items": [ {"id":"...", "docstring":"...", "new_docstring":"..."} , ... ]
      }
    """
    payload: Dict[str, Any] = (getattr(task_like, "payload", None) or task_like or {})
    payload.update(kwargs or {})
    raw_items = list(payload.get("items") or [])

    normed = [_normalize_item_for_patch(it) for it in raw_items if isinstance(it, dict) and it.get("id") is not None]
    return {"items": normed}


def verify_batch_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Trivial verifier: pass-through all items that have a non-empty docstring/new_docstring.

    Payload:
      { "items": [ ... ] }

    Returns:
      { "ok_items": [ ... ] }
    """
    payload: Dict[str, Any] = (getattr(task_like, "payload", None) or task_like or {})
    payload.update(kwargs or {})
    items = list(payload.get("items") or [])

    ok: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        val = it.get("new_docstring", it.get("docstring"))
        if isinstance(val, str) and val.strip():
            ok.append(it)

    return {"ok_items": ok}
