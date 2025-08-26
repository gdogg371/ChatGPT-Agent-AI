#v2\backend\core\prompt_pipeline\executor\providers.py
from __future__ import annotations
r"""
Executor providers used by the prompt_pipeline Engine.

This module exposes three Spine capability targets referenced in capabilities.yml:
- retriever.enrich.v1          → enrich_v1
- prompts.build.v1             → build_prompts_v1
- results.unpack.v1            → unpack_results_v1   (HARDENED JSON PARSING)

Why this change
- Pipelines were failing with “Unbalanced JSON braces in response” originating
  from unpacking. We now use the robust parser to tolerate wrappers / minor
  imbalance instead of raising, so the Engine can proceed.

Notes
- No environment variables. All behavior is driven by payload inputs.
- Keep return shapes stable to avoid breaking downstream stages.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from v2.backend.core.spine.contracts import Artifact, Task
from v2.backend.core.prompt_pipeline.llm.response_parser import parse_json_response
from .prompts import build_system_prompt, build_user_prompt, PromptMessages, make_messages


# ----------------------------- Artifact helpers --------------------------------

def _ok(uri: str, meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri=uri, sha256="", meta=meta)]


def _ng(uri: str, code: str, message: str, *, retryable: bool = False, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [Artifact(kind="Problem", uri=uri, sha256="", meta={"problem": {
        "code": code, "message": message, "retryable": retryable, "details": details or {}
    }})]


# ----------------------------- retriever.enrich.v1 ------------------------------

def enrich_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Pass-through enrichment hook. Accepts 'items' and returns them unchanged.
    Kept to satisfy capability graph without introducing side-effects.
    """
    p = task.payload or {}
    items = p.get("items") or []
    if not isinstance(items, list):
        return _ng("spine://problem/retriever.enrich.v1", "InvalidPayload", "items must be a list")
    return _ok("spine://result/retriever.enrich.v1", {"items": items})


# ----------------------------- prompts.build.v1 --------------------------------

def build_prompts_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Build system/user messages from inputs. Expects:
      payload: {
        "items": [ {...}, ... ],
        "ask_spec": { ... }                # may request response_format: json
      }
    """
    p = task.payload or {}
    items = p.get("items") or []
    ask_spec = dict(p.get("ask_spec") or {})

    if not isinstance(items, list) or not items:
        return _ng("spine://problem/prompts.build.v1", "InvalidPayload", "Missing or empty 'items'")

    system_text = build_system_prompt(ask_spec)
    user_text = build_user_prompt(items, ask_spec)

    msgs = make_messages(system_text, user_text)
    meta = {
        "messages": msgs.to_dict(),   # {"system": "...", "user": "..."}
        "items": items,
        "ask_spec": ask_spec,
    }
    return _ok("spine://result/prompts.build.v1", meta)


# ----------------------------- results.unpack.v1 -------------------------------

def _normalize_batch(results: Any) -> List[Dict[str, Any]]:
    """
    Accept a variety of shapes and normalize to a list[{"raw": "..."}] so that
    we can run a consistent JSON parser. Supported input forms:
      - {"result": {"raw": "..."}}
      - {"results": [{"raw": "..."}, ...]}
      - [{"raw": "..."}]
      - {"raw": "..."}
    """
    if isinstance(results, list):
        # already a list; ensure dicts with 'raw'
        out: List[Dict[str, Any]] = []
        for r in results:
            if isinstance(r, dict) and "raw" in r:
                out.append({"raw": r["raw"]})
            elif isinstance(r, str):
                out.append({"raw": r})
        return out

    if isinstance(results, dict):
        if "results" in results and isinstance(results["results"], list):
            return _normalize_batch(results["results"])
        if "result" in results and isinstance(results["result"], dict):
            r = results["result"]
            if "raw" in r:
                return [{"raw": r["raw"]}]
        if "raw" in results:
            return [{"raw": results["raw"]}]

    return []


def unpack_results_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Unpack/parse model outputs into structured dicts.

    Expected payload keys (flexible):
      - "results"  : list[{"raw": str, ...}, ...]   OR
      - "result"   : {"raw": str, ...}              OR
      - "raw"      : str

    Returns:
      Result meta: {"items": [ {...parsed...}, ... ], "errors": [ ... ]}

    Behavior:
      - Uses robust JSON parser (handles fenced blocks, extra text, minor imbalance).
      - Does NOT raise on parse errors; collects them in 'errors' and continues.
    """
    p = task.payload or {}
    batch = _normalize_batch(p) or _normalize_batch(p.get("results")) or _normalize_batch(p.get("result")) or _normalize_batch(p.get("raw"))
    if not batch:
        return _ng("spine://problem/results.unpack.v1", "InvalidPayload", "No results to unpack")

    parsed: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for i, item in enumerate(batch):
        raw = item.get("raw", "")
        try:
            data = parse_json_response(raw)
            parsed.append({"index": i, "data": data})
        except Exception as e:
            # Keep going; record the raw text for downstream inspection
            errors.append({"index": i, "error": f"{type(e).__name__}: {e}", "raw": str(raw)})

    return _ok("spine://result/results.unpack.v1", {"items": parsed, "errors": errors})




