# File: v2/backend/core/docstrings/providers.py
from __future__ import annotations

"""
Docstrings domain providers (Spine targets).

IMPORTANT: Providers return **plain objects** (no "result" wrapper).
The Spine will wrap them under meta.result automatically.

Capabilities implemented:
- build_prompts_v1(payload, context=None) -> {"messages": [...], "batch": [[...], ...]}
- sanitize_v1(payload, context=None) -> {"items": [...]}
- verify_v1(payload, context=None) -> {"items": [...]}
- context_build_v1 / context_read_source_window_v1 (minimal stubs)
"""

from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

# ---------------------------- helpers / utils --------------------------------

def _ensure_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]

def _ensure_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}

def _normalize_item_keys(item: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize common fields so downstream steps have predictable keys."""
    out = dict(item or {})
    # path aliases
    if "filepath" not in out and "path" in out:
        out["filepath"] = out["path"]
    if "filepath" not in out and "file" in out:
        out["filepath"] = out["file"]
    # line number
    if "lineno" not in out and "line" in out:
        out["lineno"] = out["line"]
    # symbol_type name parity
    if "symbol_type" not in out and "filetype" in out:
        out["symbol_type"] = out["filetype"]
    return out

def _normalize_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_normalize_item_keys(i) for i in items if isinstance(i, dict)]

def _drop_excluded(items: List[Dict[str, Any]], exclude_globs: List[str]) -> List[Dict[str, Any]]:
    """Drop items whose filepath matches any exclude glob."""
    if not exclude_globs:
        return items
    import fnmatch
    out: List[Dict[str, Any]] = []
    for it in items:
        fp = it.get("filepath") or it.get("path") or ""
        if any(fnmatch.fnmatch(fp, g) for g in exclude_globs):
            continue
        out.append(it)
    return out

def _truncate(s: str, n: int = 1000) -> str:
    if not isinstance(s, str):
        return ""
    return s if len(s) <= n else s[: n - 3] + "..."

# ---------------------------- prompt building --------------------------------

STRICT_JSON_CONTRACT = {
    "role": "system",
    "content": (
        "You MUST respond with EXACTLY ONE JSON object and nothing else. "
        "No prose, no explanations, no markdown, no code fences.\n\n"
        "Schema (one of the following per item):\n"
        "{\n"
        "  \"items\": [\n"
        "    {\n"
        "      \"filepath\": \"path/to/file.py\",\n"
        "      \"edits\": [\n"
        "        {\"type\": \"insert\",  \"lineno\": <int>,                        \"text\": \"DOCSTRING...\\n\"}\n"
        "        // or\n"
        "        {\"type\": \"replace\", \"start_line\": <int>, \"end_line\": <int>, \"text\": \"DOCSTRING...\\n\"}\n"
        "      ]\n"
        "    },\n"
        "    {\n"
        "      \"filepath\": \"path/to/file.py\",\n"
        "      \"docstring\": {\"lineno\": <int>, \"text\": \"DOCSTRING...\\n\"}\n"
        "    },\n"
        "    {\n"
        "      \"filepath\": \"path/to/file.py\",\n"
        "      \"patch\": \"<unified diff string>\"\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "The docstring text must include triple quotes and a trailing newline.\n"
        "If you have no changes, reply with: {\"items\": []}"
    ),
}

SYSTEM_BEHAVIOR = {
    "role": "system",
    "content": (
        "You are an expert Python tooling assistant that writes concise, accurate docstrings. "
        "Propose patches as JSON instructions only (per the strict contract)."
    ),
}

def _build_user_prompt_from_item(item: Dict[str, Any]) -> str:
    """Construct a compact user prompt for a single target."""
    fp = item.get("filepath") or item.get("path") or "UNKNOWN_FILE"
    name = item.get("name") or item.get("symbol") or item.get("id") or "<unknown symbol>"
    lineno = item.get("lineno") or item.get("line") or 0
    description = item.get("description") or ""
    ctx = _ensure_dict(item.get("context"))
    signature = ctx.get("signature") or item.get("signature") or ""
    src = ctx.get("source") or ctx.get("src") or ""
    src_snippet = _truncate(src, 1800)

    instructions = (
        "Add or fix the Python docstring for the referenced symbol. "
        "Keep it accurate, PEP 257 compliant, and do not change code behavior."
    )

    example = (
        "{\n"
        "  \"items\": [\n"
        "    {\n"
        f"      \"filepath\": \"{fp}\",\n"
        "      \"edits\": [\n"
        "        {\"type\": \"insert\", \"lineno\": <line_to_insert>, "
        "\"text\": \"\\\"\\\"\\\"Docstring...\\\"\\\"\\\"\\n\"}\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}"
    )

    parts = [
        f"Target file: {fp}",
        f"Symbol: {name} (line {lineno})",
    ]
    if description:
        parts.append(f"Current description: {description}")
    if signature:
        parts.append(f"Signature: {signature}")
    if src_snippet:
        parts.append("Source snippet:\n" + src_snippet)

    parts.append("\nInstructions:\n" + instructions)
    parts.append("\nReturn EXACTLY one JSON object with an `items` array. No markdown. Example:\n" + example)

    return "\n".join(parts)

def build_prompts_v1(payload: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Capability: docstrings.prompts.build.v1  (wired via prompts.build.v1)
    Build both a single message stream and per-item batches.

    Input:
      - items: list of records (from enrich) with fields like filepath, name, lineno, context: {source, signature}
      - exclude_globs, segment_excludes: optional filters

    Returns (PLAIN object; no 'result' wrapper):
      {"messages": [...], "batch": [[...], [...], ...]}
    """
    items = _normalize_items(_ensure_list(payload.get("items")))
    items = _drop_excluded(items, payload.get("exclude_globs") or [])

    # Per-item batches
    batch: List[List[Dict[str, Any]]] = []
    for it in items:
        seq: List[Dict[str, Any]] = [
            SYSTEM_BEHAVIOR,
            STRICT_JSON_CONTRACT,
            {"role": "user", "content": _build_user_prompt_from_item(it)},
        ]
        batch.append(seq)

    # Single combined stream
    combined_user_payload = "\n\n---\n\n".join([_build_user_prompt_from_item(it) for it in items]) or \
                            "No targets provided. If you have no changes, respond with {\"items\": []}."
    messages: List[Dict[str, Any]] = [
        SYSTEM_BEHAVIOR,
        STRICT_JSON_CONTRACT,
        {"role": "user", "content": combined_user_payload},
    ]

    return {"messages": messages, "batch": batch}

# ---------------------------- sanitize / verify ------------------------------

def _coerce_single_edit(edit_like: Dict[str, Any], fallback_lineno: int) -> Optional[Dict[str, Any]]:
    """Accept common edit shapes and coerce into a standard edit dict."""
    if not isinstance(edit_like, dict):
        return None

    # Already standard
    etype = edit_like.get("type")
    if etype in {"insert", "replace"} and ("text" in edit_like):
        return edit_like

    # docstring object → insert
    if "docstring" in edit_like and isinstance(edit_like["docstring"], dict):
        d = edit_like["docstring"]
        text = d.get("text")
        if not isinstance(text, str):
            return None
        lineno = int(d.get("lineno") or fallback_lineno or 1)
        return {"type": "insert", "lineno": lineno, "text": text}

    # direct fields {lineno,text}
    if "text" in edit_like and ("lineno" in edit_like or "line" in edit_like):
        lineno = int(edit_like.get("lineno") or edit_like.get("line") or fallback_lineno or 1)
        return {"type": "insert", "lineno": lineno, "text": edit_like["text"]}

    # replace via start/end lines
    if "start_line" in edit_like and "end_line" in edit_like and "text" in edit_like:
        return {
            "type": "replace",
            "start_line": int(edit_like["start_line"]),
            "end_line": int(edit_like["end_line"]),
            "text": edit_like["text"],
        }

    return None

def _materialize_edits(item: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    """
    From a flexible item shape, produce a canonical edits list.
    Returns (edits, ok_flag)
    """
    edits: List[Dict[str, Any]] = []

    # 1) explicit list of edits
    raw_edits = item.get("edits")
    if isinstance(raw_edits, list):
        fallback_lineno = int(item.get("lineno") or 1)
        for ed in raw_edits:
            coerced = _coerce_single_edit(ed, fallback_lineno)
            if coerced:
                edits.append(coerced)

    # 2) single edit object
    elif isinstance(raw_edits, dict):
        coerced = _coerce_single_edit(raw_edits, int(item.get("lineno") or 1))
        if coerced:
            edits.append(coerced)

    # 3) docstring object at top-level
    if not edits and isinstance(item.get("docstring"), dict):
        d = item["docstring"]
        text = d.get("text")
        if isinstance(text, str):
            lineno = int(d.get("lineno") or item.get("lineno") or 1)
            edits.append({"type": "insert", "lineno": lineno, "text": text})

    # 4) plain text docstring fields
    if not edits and isinstance(item.get("text"), str):
        lineno = int(item.get("lineno") or 1)
        edits.append({"type": "insert", "lineno": lineno, "text": item["text"]})

    # ok if we have edits OR a textual patch/diff
    ok = bool(edits) or isinstance(item.get("patch") or item.get("diff"), str)
    return edits, ok

def sanitize_v1(payload: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Capability: docstrings.sanitize.v1
    Accepts: {"items": [...]} from results.unpack.v1
    Returns (PLAIN): {"items": [...]}
    """
    raw_items = _ensure_list(_ensure_dict(payload).get("items"))
    exclude_globs = payload.get("exclude_globs") or []
    items = _normalize_items([i for i in raw_items if isinstance(i, dict)])

    sanitized: List[Dict[str, Any]] = []
    for it in items:
        fp = it.get("filepath") or it.get("path") or it.get("file")
        if not fp:
            continue

        edits, ok_flag = _materialize_edits(it)
        if not ok_flag:
            # nothing usable; skip
            continue

        if edits:
            it = dict(it)
            it["edits"] = edits

        sanitized.append(it)

    sanitized = _drop_excluded(sanitized, exclude_globs)
    return {"items": sanitized}

def verify_v1(payload: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Capability: docstrings.verify.v1
    Performs minimal structural checks and keeps OK items.
    Returns (PLAIN): {"items": [...]}
    """
    items = _normalize_items(_ensure_list(_ensure_dict(payload).get("items")))
    project_root = payload.get("project_root")
    root_path: Optional[Path] = Path(project_root).resolve() if isinstance(project_root, str) else None

    ok: List[Dict[str, Any]] = []
    for it in items:
        fp = it.get("filepath")
        if not fp:
            continue
        # Optional existence check (best-effort)
        if root_path:
            try:
                candidate = (root_path / fp).resolve()
                # Keep only within root; do not reject solely on non-existence (mirrors allowed)
                if root_path not in candidate.parents and candidate != root_path:
                    continue
            except Exception:
                pass
        edits = it.get("edits")
        patch = it.get("patch") or it.get("diff")
        if edits is None and not patch:
            continue
        ok.append(it)

    return {"items": ok}

# ---------------------------- optional context -------------------------------

def context_build_v1(payload: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Optional adapter hook; no-op context."""
    return {"context": payload or {}}

def context_read_source_window_v1(payload: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Optional adapter hook; no-op helper."""
    return {"window": payload or {}}


