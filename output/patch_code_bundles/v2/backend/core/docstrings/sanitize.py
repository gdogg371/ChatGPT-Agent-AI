# v2/backend/core/docstrings/sanitize.py
from __future__ import annotations

import os
import re
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---- Artifact shim (works even if spine.contracts isn't importable yet) ----

try:
    from v2.backend.core.spine.registry import register  # type: ignore
    register("sanitize.v1", "v2.backend.core.docstrings.sanitize:sanitize_outputs_v1")
except Exception:
    pass


try:
    from v2.backend.core.spine.contracts import Artifact  # type: ignore
except Exception:
    class Artifact:  # type: ignore
        def __init__(self, kind: str, uri: str, sha256: str = "", meta: Dict[str, Any] | None = None):
            self.kind = kind
            self.uri = uri
            self.sha256 = sha256
            self.meta = meta or {}


def _ok(uri: str, meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri=uri, sha256="", meta=meta)]


def _problem(uri: str, code: str, msg: str, *, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [Artifact(kind="Problem", uri=uri, sha256="", meta={
        "problem": {"code": code, "message": msg, "retryable": False, "details": details or {}}
    })]


def _task_payload(task_like: Any) -> Dict[str, Any]:
    if isinstance(task_like, dict):
        return task_like
    for a in ("payload", "meta", "data"):
        v = getattr(task_like, a, None)
        if isinstance(v, dict):
            return v
    return {}


# ---------------- docstring formatting helpers ----------------

def _normalize_lines(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in s.split("\n")]  # strip trailing spaces
    out, blanks = [], 0
    for ln in lines:
        if ln.strip() == "":
            blanks += 1
            if blanks <= 2:  # collapse 3+ blanks
                out.append("")
            # else drop
        else:
            blanks = 0
            out.append(ln)
    return "\n".join(out).strip()


def _split_summary_body(s: str) -> Tuple[str, str]:
    s = s.strip()
    parts = re.split(r"\n\s*\n", s, maxsplit=1)  # paragraph split on blank line
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    m = re.search(r"([.!?])(\s+|$)", s)  # first sentence terminator
    if m and m.end() < len(s):
        return s[:m.end()].strip(), s[m.end():].strip()
    return s, ""


def _wrap_paragraph(text: str, width: int) -> str:
    if not text.strip():
        return ""
    lines = text.split("\n")
    out: List[str] = []
    para: List[str] = []

    def flush():
        if not para:
            return
        block = " ".join(x.strip() for x in para).strip()
        out.append(textwrap.fill(block, width=width, break_long_words=False, break_on_hyphens=False))
        para.clear()

    for ln in lines:
        if ln.strip().startswith(("- ", "* ")):
            # bullets: keep, indent continuation
            flush()
            bullet, rest = ln[:2], ln[2:].strip()
            wrapped = textwrap.fill(
                rest,
                width=max(4, width - 2),
                subsequent_indent="  ",
                break_long_words=False,
                break_on_hyphens=False,
            )
            parts = wrapped.split("\n")
            if parts:
                out.append(bullet + parts[0])
                out.extend("  " + p for p in parts[1:])
            else:
                out.append(bullet)
        elif ln.strip() == "":
            flush()
            out.append("")
        else:
            para.append(ln)
    flush()

    # collapse extra blanks + rstrip again
    cleaned: List[str] = []
    blanks = 0
    for ln in out:
        if ln == "":
            blanks += 1
            if blanks <= 2:
                cleaned.append("")
        else:
            blanks = 0
            cleaned.append(ln.rstrip())
    return "\n".join(cleaned).strip()


# Correct triple-quote regex with named groups
_TRIPLE_QUOTE_RE = re.compile(r'^\s*(?P<q>("""|\'\'\'))(?P<body>.*?)(?P=q)\s*$', re.S)


def _strip_triple_quotes(raw: str) -> str:
    m = _TRIPLE_QUOTE_RE.match(raw)
    return m.group("body") if m else raw


def _format_docstring_content(raw: str, width: int = 72) -> str:
    """
    Returns the *inner* docstring content that goes between the triple quotes.

    Guarantees:
    • summary line wrapped to width; blank line before body if body exists
    • no trailing spaces
    • content starts after opening quotes and ends before closing (single leading/trailing newline)
    """
    raw = _strip_triple_quotes(raw)
    norm = _normalize_lines(raw)
    summary, body = _split_summary_body(norm)

    summary_wrapped = _wrap_paragraph(summary, width)
    body_wrapped = _wrap_paragraph(body, width) if body else ""

    inner = summary_wrapped + (("\n\n" + body_wrapped) if body_wrapped else "")
    inner = inner.strip()
    # renderer expects one leading and one trailing newline
    return "\n" + inner + "\n"


def _format_docstrings_inplace(items: List[Dict[str, Any]], width: int) -> None:
    for it in items:
        ds = str(it.get("docstring") or "")
        it["docstring"] = _format_docstring_content(ds, width=width)


# ------------- sanitize: self-contained, no imports from other local modules ---

def sanitize_outputs_v1(task: Any, context: Dict[str, Any] | None = None) -> List[Artifact]:
    """
    Capability: docstrings.sanitize.v1

    Self-contained sanitizer that:
    • merges LLM results (id → docstring) with prepared_batch metadata,
    • formats docstring content (wrapped lines, summary+blank line+body),
    • normalizes paths and fields required by the patch engine.

    Input payload:
    {
      "items": {
        "<id>": {"docstring": "..."}
      } OR [{"id": "...", "docstring": "..."}],
      "prepared_batch": [
        {id, relpath|filepath, path?, signature?, target_lineno?, has_docstring?}
      ],
      "project_root": "..."
    }

    Output meta:
    {
      "result": [
        {id, docstring, relpath, path, target_lineno, signature, has_docstring}
      ]
    }
    """
    payload = _task_payload(task)
    project_root = str(payload.get("project_root") or "").strip()
    prepared_batch = list(payload.get("prepared_batch") or payload.get("batch") or [])

    # Build a quick index by id from prepared_batch
    idx: Dict[str, Dict[str, Any]] = {}
    for it in prepared_batch:
        try:
            idx[str(it.get("id"))] = dict(it)
        except Exception:
            continue

    # Normalize input items: support dict form or list form
    raw_items = payload.get("items") or []
    items_list: List[Dict[str, Any]] = []
    if isinstance(raw_items, dict):
        for k, v in raw_items.items():
            if isinstance(v, dict):
                items_list.append({"id": str(k), **v})
            else:
                items_list.append({"id": str(k), "docstring": str(v)})
    elif isinstance(raw_items, list):
        items_list = [dict(x) for x in raw_items if isinstance(x, dict)]
    else:
        return _ok("spine://docstrings/sanitize_v1", {"result": []})

    # Format docstrings
    width_env = os.environ.get("DOCSTRING_WIDTH", "72")
    try:
        width = max(1, int(width_env))
    except Exception:
        width = 72
    _format_docstrings_inplace(items_list, width=width)

    # Merge with prepared metadata and normalize paths
    out: List[Dict[str, Any]] = []
    root_path = Path(project_root) if project_root else None

    for it in items_list:
        _id = str(it.get("id") or "").strip()
        if not _id:
            continue  # skip items without id
        meta = idx.get(_id, {})

        rel = (meta.get("relpath") or meta.get("filepath") or it.get("relpath") or "").strip().replace("\\", "/")
        # remove leading "./" or "/" to keep it repo-relative
        while rel.startswith("./"):
            rel = rel[2:]
        if rel.startswith("/"):
            rel = rel[1:]

        # Determine absolute path
        abs_path = meta.get("path") or it.get("path") or ""
        if not abs_path and rel:
            if root_path:
                abs_path = str((root_path / rel).resolve())
            else:
                abs_path = rel  # best effort

        sanitized = {
            "id": _id,
            "docstring": it.get("docstring") or "",
            "relpath": rel,
            "path": abs_path,
            "target_lineno": int(meta.get("target_lineno") or it.get("target_lineno") or 1),
            "signature": meta.get("signature") or it.get("signature") or "module",
            "has_docstring": bool(meta.get("has_docstring") or it.get("has_docstring") or False),
        }
        out.append(sanitized)

    return _ok("spine://docstrings/sanitize_v1", {"result": out})


# Optional: registry registration (harmless if registry not present)
try:
    from v2.backend.core.spine.registry import register as spine_register  # type: ignore
    spine_register("docstrings.sanitize.v1", sanitize_outputs_v1)
except Exception:
    pass


__all__ = ["sanitize_outputs_v1"]


