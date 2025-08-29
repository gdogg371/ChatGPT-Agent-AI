# File: v2/backend/core/prompt_pipeline/executor/steps.py
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from v2.backend.core.prompt_pipeline.executor.errors import ValidationError


# ------------------------------ prompt builders -------------------------------

def build_system_prompt() -> str:
    """ Minimal, self-contained system prompt for docstring generation.
    Keeps this module independent of other packages.
    """
    return (
        "You are a precise code documentation assistant."
        "For each Python target, generate a concise, accurate docstring that:\n"
        " - Explains purpose and behavior\n"
        " - Documents params/returns/raises when relevant\n"
        " - Uses the project's prevailing style (triple-quoted, reST or Google style)\n"
        " - Fits within 20 lines unless absolutely necessary\n"
        "Return ONLY JSON with this shape:\n"
        "{\n"
        '  "items": [\n'
        '    { "id": "", "mode": "create|rewrite", "docstring": "" }\n'
        "  ]\n"
        "}\n"
        "No extra commentary or markdown fences."
    )


def build_user_prompt(batch: List[Dict[str, Any]]) -> str:
    """ Build the user prompt by embedding a slimmed view of items
    (id, mode, signature, description, contextual code).

    The model must return JSON as instructed in the system prompt.
    """
    slim = [
        {
            "id": it.get("id"),
            "mode": it.get("mode", "rewrite"),
            "signature": it.get("signature"),
            "description": (it.get("description") or "")[:1000],
            "has_docstring": bool(it.get("has_docstring", False)),
            "existing_docstring": (it.get("existing_docstring") or "")[:1200],
            "context": (it.get("context_code") or "")[:2000],
        }
        for it in batch
    ]
    return (
        "Targets to document (JSON):\n"
        + json.dumps({"items": slim}, ensure_ascii=False)
        + "\n\nProduce JSON in the exact schema from the system prompt."
    )


# ------------------------------ context & packing ------------------------------

@dataclass
class BuildContextStep:
    def run(self, suspects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """ Convert suspect rows into docstring work items expected by the packer.
        This keeps the step independent from specific task adapters.
        """
        items: List[Dict[str, Any]] = []
        for s in suspects:
            has = bool(s.get("has_docstring")) and bool(s.get("existing_docstring"))
            mode = "rewrite" if has else "create"
            items.append(
                {
                    "id": str(s.get("id")),
                    "relpath": s.get("relpath"),
                    "path": s.get("path"),
                    "signature": s.get("signature"),
                    "target_lineno": int(s.get("target_lineno") or s.get("lineno") or 0),
                    "mode": mode,
                    "has_docstring": bool(s.get("has_docstring", False)),
                    "existing_docstring": s.get("existing_docstring", ""),
                    "description": s.get("description", ""),
                    "context_code": s.get("context_code", ""),
                }
            )
        return items


@dataclass
class PackPromptStep:
    def serialize_items(self, items: List[Dict[str, Any]]) -> str:
        slim = [
            {
                "id": it["id"],
                "mode": it.get("mode", "rewrite"),
                "sig": it.get("signature"),
                "desc": (it.get("description") or "")[:1000],
                "ctx": (it.get("context_code") or "")[:2000],
                "has": bool(it.get("has_docstring", False)),
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


# --------------------- Robust JSON extraction & salvage ------------------------

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
                out.append(ch)
                esc = False
                continue
            if ch == "\\":
                out.append(ch)
                esc = True
                continue
            if ch == '"':
                out.append(ch)
                in_str = False
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
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
        objs.append(s[i : j + 1])
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
        return obj  # type: ignore[return-value]

    # Try a balanced top-level object
    i, j = _balanced_slice(cleaned, 0)
    if i >= 0 and j >= 0:
        cand = cleaned[i : j + 1]
        obj = _try_json_loads(cand)
        if obj is None:
            repaired = _escape_raw_newlines_in_strings(cand)
            repaired = _strip_trailing_commas(repaired)
            obj = _try_json_loads(repaired)
            if obj is not None:
                return obj  # type: ignore[return-value]

    # No valid root; give up here and let caller run salvage-by-objects.
    raise ValidationError("Unbalanced JSON braces in response")


def _salvage_items_from_text(raw: str, expected_ids: List[str]) -> List[Dict[str, Any]]:
    """ Last-resort recovery: scan for ANY balanced dicts and pick those that look like items.
    This lets us recover partial results when the outer JSON is truncated.
    """
    cleaned = _normalize_quotes(_strip_code_fences(raw))
    objs = _extract_all_objects(cleaned)
    results: List[Dict[str, Any]] = []
    for cand in objs:
        # attempt parse with repairs
        obj_any = _try_json_loads(cand) or _try_json_loads(
            _strip_trailing_commas(_escape_raw_newlines_in_strings(cand))
        )
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
            return "items", obj["items"]  # type: ignore[return-value]
        if isinstance(obj.get("results"), list):
            return "results", obj["results"]  # type: ignore[return-value]
    raise ValidationError("Response JSON missing 'items' or 'results' list")


# ------------------------------ unpack results --------------------------------

@dataclass
class UnpackResultsStep:
    expected_ids: List[str]

    def run(self, raw: str) -> List[Dict[str, Any]]:
        """ Parse the model's response into a list of items:
        {
          "id": str,
          "mode": "create|rewrite",
          "docstring": str,
          "extras": {...}
        }
        Strict parsing first; if nothing valid is found, attempt salvage.
        """
        try:
            data = _coerce_to_json_dict(raw)
            _, rows = _extract_candidates(data)

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
            # Strict parse failed due to structural issues; attempt salvage.
            salv = _salvage_items_from_text(raw, self.expected_ids)
            if not salv:
                raise
            return salv


# ------------------------------ static self-test -------------------------------

if __name__ == "__main__":
    """
    Minimal static tests (no LLM calls):
      1) Build prompts for a small batch → ensure messages exist.
      2) Unpack well-formed JSON.
      3) Salvage from messy text with code fences/smart quotes/trailing commas.
    Exits non-zero on validation failures.
    """
    failures = 0

    # 1) Pack prompts
    batch = [
        {
            "id": "A",
            "mode": "create",
            "signature": "def foo(x: int) -> int",
            "description": "Compute foo.",
            "context_code": "def foo(x: int) -> int:\n    return x + 1\n",
            "has_docstring": False,
        },
        {
            "id": "B",
            "mode": "rewrite",
            "signature": "class Bar: ...",
            "existing_docstring": "Old doc",
            "context_code": "class Bar:\n    pass\n",
            "has_docstring": True,
        },
    ]
    pack = PackPromptStep().build(batch)
    ok_pack = bool(pack.get("messages", {}).get("system")) and bool(pack.get("messages", {}).get("user"))
    print("[steps.selftest] pack:", "OK" if ok_pack else "FAIL")
    failures += 0 if ok_pack else 1

    # 2) Unpack well-formed JSON
    expected_ids = [it["id"] for it in batch]
    good_json = json.dumps(
        {"items": [{"id": "A", "mode": "create", "docstring": "Doc A"}, {"id": "B", "mode": "rewrite", "docstring": "Doc B"}]},
        ensure_ascii=False,
    )
    parsed = UnpackResultsStep(expected_ids=expected_ids).run(good_json)
    ok_parse = {p["id"] for p in parsed} == set(expected_ids)
    print("[steps.selftest] parse:", "OK" if ok_parse else "FAIL")
    failures += 0 if ok_parse else 1

    # 3) Salvage from messy text (code fence + smart quotes + trailing commas)
    messy = """```json
    { “items”: [
        {"id": "A", "docstring": "Doc A",},
        {"id": "B", "mode": "rewrite", "docstring": "Doc B"}
      ]
    }
    ```
    extra chatter...
    """
    salv = UnpackResultsStep(expected_ids=expected_ids).run(messy)
    ok_salv = {p["id"] for p in salv} == set(expected_ids)
    print("[steps.selftest] salvage:", "OK" if ok_salv else "FAIL")
    failures += 0 if ok_salv else 1

    raise SystemExit(failures)



