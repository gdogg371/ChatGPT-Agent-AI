# File: v2/backend/core/prompt_pipeline/executor/engine.py
"""
Generic LLM patch engine (v1), error-surfacing + zero-record promotion.

Return shape:
{
  "run_dir": str|null,
  "counts": {"built_messages": int, "built_batch": int, "sanitized": int, "verified": int},
  "problems": [ ... ],
  "debug": {
    "fetch_meta": { ... }   # minimal info to diagnose FETCH
  }
}
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .orchestrator import capability_run  # type: ignore


# ------------------------------- utils ----------------------------------------


def _first(lst: Any) -> Any:
    return lst[0] if isinstance(lst, list) and lst else None


def _artifact_to_dict(a: Any) -> Dict[str, Any]:
    if isinstance(a, dict):
        return {
            "kind": a.get("kind"),
            "uri": a.get("uri"),
            "sha256": a.get("sha256", ""),
            "meta": a.get("meta") if isinstance(a.get("meta"), dict) else {},
        }
    return {
        "kind": getattr(a, "kind", None),
        "uri": getattr(a, "uri", None),
        "sha256": getattr(a, "sha256", "") or "",
        "meta": getattr(a, "meta", {}) if isinstance(getattr(a, "meta", {}), dict) else {},
    }


def _unwrap_meta(maybe_arts: Any) -> Dict[str, Any]:
    if maybe_arts is None:
        return {}
    if isinstance(maybe_arts, list):
        first = _first(maybe_arts)
        if first is None:
            return {}
        if isinstance(first, str):
            return {"error": "Unexpected string artifact", "raw": first}
        d = _artifact_to_dict(first)
        return d.get("meta") or {}
    if isinstance(maybe_arts, dict):
        if "kind" in maybe_arts and "uri" in maybe_arts and "meta" in maybe_arts:
            return maybe_arts.get("meta") or {}
        return maybe_arts
    return {"error": "Unexpected result", "raw": repr(maybe_arts)}


def _get_from_meta(meta: Dict[str, Any], *path: str, default: Any = None) -> Any:
    cur: Any = meta
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def _get_records(fetch_result: Any) -> List[Dict[str, Any]]:
    meta = _unwrap_meta(fetch_result)
    recs = meta.get("records")
    if isinstance(recs, list):
        return [r for r in recs if isinstance(r, dict)]
    recs = _get_from_meta(meta, "result", "records")
    if isinstance(recs, list):
        return [r for r in recs if isinstance(r, dict)]
    return []


def _collect_problems(arts: Any, capability: str, phase: str) -> List[Dict[str, Any]]:
    problems: List[Dict[str, Any]] = []

    def _as_dict(a: Any) -> Dict[str, Any]:
        if isinstance(a, dict):
            return a
        return {
            "kind": getattr(a, "kind", None),
            "uri": getattr(a, "uri", None),
            "meta": getattr(a, "meta", None),
        }

    if isinstance(arts, list):
        for a in arts:
            d = _as_dict(a)
            kind = d.get("kind")
            meta = d.get("meta") or {}
            if kind == "Problem" or ("problem" in meta) or ("error" in meta and "message" in meta):
                problems.append({
                    "phase": phase,
                    "capability": capability,
                    "kind": kind,
                    "uri": d.get("uri"),
                    "problem": meta.get("problem"),
                    "error": meta.get("error"),
                    "message": meta.get("message"),
                    "exception": meta.get("exception"),
                    "traceback": meta.get("traceback"),
                    "meta": {k: v for k, v in meta.items()
                             if k not in {"problem", "error", "message", "exception", "traceback"}},
                })
    elif isinstance(arts, dict):
        if arts.get("kind") == "Problem" or "problem" in arts or "error" in arts:
            problems.append({"phase": phase, "capability": capability, **arts})
    return problems


def _payload_of(task_or_name: Any, payload: Optional[Dict[str, Any]], context: Optional[Dict[str, Any]]) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    if hasattr(task_or_name, "payload") and isinstance(getattr(task_or_name, "payload"), dict):
        return "llm.engine.run.v1", dict(task_or_name.payload), dict(context or {})
    if isinstance(task_or_name, str):
        return task_or_name, dict(payload or {}), dict(context or {})
    if isinstance(task_or_name, dict):
        return "llm.engine.run.v1", dict(task_or_name), dict(context or {})
    return "llm.engine.run.v1", {}, dict(context or {})


def _log(name: str, msg: str) -> None:
    print(f"[{name}] {msg}")


# --------------------------------- engine ------------------------------------


def run_v1(task_or_name: Any, payload: Optional[Dict[str, Any]] = None, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    name, pl, ctx = _payload_of(task_or_name, payload, context)
    _log(name, "engine ACTIVE (bundle logging + prompt inject via Spine)")

    problems: List[Dict[str, Any]] = []
    continue_on_error: bool = bool(pl.get("continue_on_error", False))
    require_records: bool = bool(pl.get("require_records", True))

    # ------------------------------- FETCH ------------------------------------
    _log("PHASE", "FETCH")
    fetch_payload = {
        "sqlalchemy_url": pl.get("sqlalchemy_url"),
        "sqlalchemy_table": pl.get("sqlalchemy_table"),
        "status": pl.get("status", pl.get("status_filter")),
        "status_any": pl.get("status_any"),
        "max_rows": pl.get("max_rows", 50),
        "columns": pl.get("columns"),
    }
    fetch_res = capability_run("introspect.fetch.v1", fetch_payload, {"phase": "FETCH"})
    problems += _collect_problems(fetch_res, "introspect.fetch.v1", "FETCH")
    fetch_meta = _unwrap_meta(fetch_res)
    records = _get_records(fetch_res)
    _log("FETCH", f"records={len(records)}")

    # Promote empty fetch to a Problem (optional, controlled by require_records)
    if not records and require_records:
        problems.append({
            "phase": "FETCH",
            "capability": "introspect.fetch.v1",
            "kind": "Problem",
            "uri": "spine://capability/introspect.fetch.v1",
            "problem": {
                "code": "NoRecords",
                "message": "Fetch returned zero rows; cannot proceed.",
                "retryable": False,
                "details": {
                    "table": pl.get("sqlalchemy_table"),
                    "status": pl.get("status", pl.get("status_filter")),
                    "status_any": pl.get("status_any"),
                    "max_rows": pl.get("max_rows", 50),
                },
            },
            "meta": {k: v for k, v in fetch_meta.items() if k in {"error", "query", "params", "count"}},
        })
        if not continue_on_error:
            return {
                "run_dir": None,
                "counts": {"built_messages": 0, "built_batch": 0, "sanitized": 0, "verified": 0},
                "problems": problems,
                "debug": {"fetch_meta": {k: v for k, v in fetch_meta.items() if k in {"error", "query", "params", "count"}}},
            }

    # ------------------------------- ENRICH -----------------------------------
    _log("PHASE", "ENRICH")
    _log("ENRICH", f"items={len(records)}")

    # --------------------------- CONTEXT.BUILD --------------------------------
    _log("PHASE", "CONTEXT.BUILD")

    # -------------------------------- BUILD -----------------------------------
    _log("PHASE", "BUILD")
    build_payload = {
        "items": records,
        "ask_spec": pl.get("ask_spec", {}),
        "exclude_globs": pl.get("exclude_globs", []),
        "segment_excludes": pl.get("segment_excludes", []),
    }
    build_res = capability_run("prompts.build.v1", build_payload, {"phase": "BUILD"})
    problems += _collect_problems(build_res, "prompts.build.v1", "BUILD")
    build_meta = _unwrap_meta(build_res)
    built_messages = 1 if (records and build_meta) else 0
    built_batch = len(_get_from_meta(build_meta, "result", "batch", default=[])) if build_meta else 0

    # --------------------------- BUNDLE.INJECT --------------------------------
    _log("PHASE", "BUNDLE.INJECT")
    inj_res = capability_run("packager.bundle.inject_prompt.v1", {"items": records}, {"phase": "BUNDLE.INJECT"})
    problems += _collect_problems(inj_res, "packager.bundle.inject_prompt.v1", "BUNDLE.INJECT")

    # --------------------------------- LLM ------------------------------------
    _log("PHASE", "LLM")
    if built_messages or records:
        ask_res = capability_run("llm.ask.v1", {"items": records, "ask_spec": pl.get("ask_spec", {})}, {"phase": "LLM"})
        problems += _collect_problems(ask_res, "llm.ask.v1", "LLM")

    # ------------------------------- SANITIZE ---------------------------------
    _log("PHASE", "SANITIZE")
    sanitize_res = capability_run("sanitize.v1", {"items": records}, {"phase": "SANITIZE"})
    problems += _collect_problems(sanitize_res, "sanitize.v1", "SANITIZE")
    sanitize_meta = _unwrap_meta(sanitize_res)
    sanitized_items = _get_from_meta(sanitize_meta, "items", default=[])

    _log("SANITIZE", f"items={len(sanitized_items) if isinstance(sanitized_items, list) else 0}")

    # -------------------------------- VERIFY ----------------------------------
    _log("PHASE", "VERIFY")
    verify_res = capability_run("verify.v1", {"items": sanitized_items if isinstance(sanitized_items, list) else []}, {"phase": "VERIFY"})
    problems += _collect_problems(verify_res, "verify.v1", "VERIFY")
    verify_meta = _unwrap_meta(verify_res)
    ok_items = _get_from_meta(verify_meta, "ok_items", default=[])

    _log("VERIFY", f"ok_items={len(ok_items) if isinstance(ok_items, list) else 0}")

    # -------------------------- PATCH.APPLY_FILES ------------------------------
    _log("PHASE", "PATCH.APPLY_FILES")

    final_meta = {
        "run_dir": _get_from_meta(build_meta, "result", "run_dir", default=None)
                  or _get_from_meta(sanitize_meta, "result", "run_dir", default=None)
                  or _get_from_meta(verify_meta, "result", "run_dir", default=None),
        "counts": {
            "built_messages": int(built_messages),
            "built_batch": int(built_batch),
            "sanitized": int(len(sanitized_items)) if isinstance(sanitized_items, list) else 0,
            "verified": int(len(ok_items)) if isinstance(ok_items, list) else 0,
        },
        "problems": problems,
        "debug": {
            "fetch_meta": {k: v for k, v in fetch_meta.items() if k in {"error", "query", "params", "count"}},
        },
    }
    return final_meta

