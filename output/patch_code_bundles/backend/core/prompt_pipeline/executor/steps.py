from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, List, Any, Tuple

from v2.backend.core.prompt_pipeline.executor.errors import ValidationError
from .prompts import build_system_prompt, build_user_prompt


@dataclass
class BuildContextStep:
    def run(self, suspects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for s in suspects:
            has = bool(s.get("has_docstring")) and bool(s.get("existing_docstring"))
            mode = "rewrite" if has else "create"
            items.append({
                "id": str(s["id"]),
                "relpath": s["relpath"],
                "path": s["path"],
                "signature": s["signature"],
                "target_lineno": int(s["target_lineno"]),
                "mode": mode,
                "has_docstring": bool(s.get("has_docstring")),
                "existing_docstring": s.get("existing_docstring", ""),
                "description": s.get("description", ""),
                "context_code": s.get("context_code", ""),
            })
        return items


@dataclass
class PackPromptStep:
    def serialize_items(self, items: List[Dict[str, Any]]) -> str:
        slim = [
            {
                "id": it["id"],
                "mode": it["mode"],
                "sig": it["signature"],
                "desc": it.get("description", "")[:1000],
                "ctx": it.get("context_code", "")[:2000],
                "has": it.get("has_docstring", False),
            }
            for it in items
        ]
        return json.dumps({"items": slim}, ensure_ascii=False)

    def build(self, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        system = build_system_prompt()
        user = build_user_prompt(batch)
        return {
            "messages": {"system": system, "user": user},
            "ids": [it["id"] for it in batch],
            "batch": batch,
        }


# ---------- Robust JSON extraction & salvage ----------

_FENCE_RX = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)
_SMART_QUOTES = {
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "’": "'", "‘": "'", "‚": "'", "‛": "'",
}
_TRAILING_COMMA_RX = re.compile(r",\s*([}\]])")

def _strip_code_fences(s: str) -> str:
    return _FENCE_RX.sub("", s.strip())

def _normalize_quotes(s: str) -> str:
    for a, b in _SMART_QUOTES.items():
        s = s.replace(a, b)
    return s

def _strip_trailing_commas(s: str) -> str:
    return _TRAILING_COMMA_RX.sub(r"\1", s)

def _escape_raw_newlines_in_strings(s: str) -> str:
    out: List[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                out.append(ch); esc = False; continue
            if ch == "\\":
                out.append(ch); esc = True; continue
            if ch == '"':
                out.append(ch); in_str = False; continue
            if ch == "\n":
                out.append("\\n"); continue
            if ch == "\r":
                out.append("\\r"); continue
            if ch == "\t":
                out.append("\\t"); continue
            out.append(ch)
        else:
            out.append(ch)
            if ch == '"':
                in_str = True
    return "".join(out)

def _balanced_slice(s: str, start_pos: int = 0) -> Tuple[int, int]:
    """Return (start,end) for the first balanced {...} after start_pos, ignoring braces inside strings."""
    i = s.find("{", start_pos)
    if i < 0:
        return (-1, -1)
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(s)):
        ch = s[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        else:
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return (i, j)
    return (i, -1)

def _extract_all_objects(s: str) -> List[str]:
    """Extract all balanced JSON objects from text (string-aware); return as raw substrings."""
    objs: List[str] = []
    pos = 0
    while True:
        i, j = _balanced_slice(s, pos)
        if i < 0:
            break
        if j < 0:  # unbalanced till end
            break
        objs.append(s[i:j+1])
        pos = j + 1
    return objs

def _try_json_loads(txt: str) -> Any:
    try:
        return json.loads(txt)
    except Exception:
        return None

def _coerce_to_json_dict(raw: str) -> Dict[str, Any]:
    """Strict parse → cleanup → balanced root → repairs; else raise."""
    try:
        return json.loads(raw)
    except Exception:
        pass

    cleaned = _strip_code_fences(raw)
    cleaned = _normalize_quotes(cleaned)
    obj = _try_json_loads(cleaned)
    if obj is not None:
        return obj

    # Try a balanced top-level object
    i, j = _balanced_slice(cleaned, 0)
    if i >= 0 and j >= 0:
        cand = cleaned[i:j+1]
        obj = _try_json_loads(cand)
        if obj is None:
            repaired = _escape_raw_newlines_in_strings(cand)
            repaired = _strip_trailing_commas(repaired)
            obj = _try_json_loads(repaired)
        if obj is not None:
            return obj

    # No valid root; give up here and let caller run salvage-by-objects.
    raise ValidationError("Unbalanced JSON braces in response")

def _salvage_items_from_text(raw: str, expected_ids: List[str]) -> List[Dict[str, Any]]:
    """
    Last-resort recovery: scan for ANY balanced dicts and pick those that look like items.
    This lets us recover partial results when the outer JSON is truncated.
    """
    cleaned = _normalize_quotes(_strip_code_fences(raw))
    objs = _extract_all_objects(cleaned)
    results: List[Dict[str, Any]] = []

    for cand in objs:
        # attempt parse with repairs
        obj_any = _try_json_loads(cand) or _try_json_loads(_strip_trailing_commas(_escape_raw_newlines_in_strings(cand)))
        if not isinstance(obj_any, dict):
            continue
        rid = obj_any.get("id")
        doc = obj_any.get("docstring")
        if isinstance(rid, (str, int)) and isinstance(doc, str):
            rid_str = str(rid).strip()
            if rid_str in expected_ids:
                mode = str(obj_any.get("mode", "rewrite")).lower() or "rewrite"
                extras = {k: v for k, v in obj_any.items() if k not in {"id", "docstring", "mode"}}
                results.append({"id": rid_str, "mode": mode, "docstring": doc, "extras": extras})

    # Deduplicate by id, preserve first occurrence (most complete)
    dedup: Dict[str, Dict[str, Any]] = {}
    for r in results:
        if r["id"] not in dedup:
            dedup[r["id"]] = r
    return list(dedup.values())


def _extract_candidates(obj: Any) -> Tuple[str, List[Dict[str, Any]]]:
    if isinstance(obj, dict):
        if isinstance(obj.get("items"), list):
            return "items", obj["items"]
        if isinstance(obj.get("results"), list):
            return "results", obj["results"]
    raise ValidationError("Response JSON missing 'items' or 'results' list")


@dataclass
class UnpackResultsStep:
    expected_ids: List[str]

    def run(self, raw: str) -> List[Dict[str, Any]]:
        # ... keep the call to _coerce_to_json_dict/_salvage as you have ...

        try:
            data = _coerce_to_json_dict(raw)
            key, rows = _extract_candidates(data)
            out: List[Dict[str, Any]] = []
            seen = set()
            allowed = set(self.expected_ids)

            for row in rows:
                if not isinstance(row, dict):
                    continue
                rid_raw = row.get("id", "")
                rid = str(rid_raw).strip()
                doc = row.get("docstring", "")
                mode = str(row.get("mode", "rewrite")).lower() or "rewrite"

                # Skip unexpected ids rather than failing the whole batch
                if rid not in allowed:
                    continue
                # Deduplicate: keep first occurrence
                if rid in seen:
                    continue
                if not isinstance(doc, str) or not doc.strip():
                    continue

                extras = {k: v for k, v in row.items() if k not in {"id", "docstring", "mode"}}
                out.append({"id": rid, "mode": mode, "docstring": doc, "extras": extras})
                seen.add(rid)

            # If none parsed via root, attempt salvage
            if not out:
                out = _salvage_items_from_text(raw, self.expected_ids)

            return out
        except ValidationError:
            # existing salvage path unchanged
            salv = _salvage_items_from_text(raw, self.expected_ids)
            if not salv:
                raise
            return salv
