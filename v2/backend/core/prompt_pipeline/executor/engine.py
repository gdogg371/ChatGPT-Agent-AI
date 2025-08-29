# File: v2/backend/core/prompt_pipeline/executor/engine.py
"""
General-purpose, capability-routed pipeline engine.

Phases:
  FETCH -> ENRICH -> CONTEXT.BUILD -> BUILD -> BUNDLE.MAKE/INJECT (via Spine)
  -> LLM -> SANITIZE -> VERIFY -> PATCH.APPLY_FILES -> FINALIZE

All cross-module work is performed via Spine capabilities.
This file wires phases together and persists run artifacts/output.

NOTE:
- No domain-specific (e.g., docstrings) logic lives here.
- Domain adapters are invoked via Spine (prompts.build.v1, sanitize.v1, verify.v1, etc.).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Orchestrator entry-point to call capabilities
try:
    from .orchestrator import capability_run  # type: ignore
except Exception as e:  # pragma: no cover
    raise RuntimeError("executor.engine requires executor.orchestrator.capability_run") from e


# ------------------------------- utils ---------------------------------------


@dataclass
class Norm:
    root: str
    project_root: str
    out_base: str
    out_file: str


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _write_json(p: Path, obj: Any) -> None:
    try:
        _ensure_dir(p.parent)
        p.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        # best-effort logging; engine must not crash on artifact writes
        pass


def _now_token() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _normalize(payload: Dict[str, Any]) -> Norm:
    root = str(Path(payload.get("root") or payload.get("project_root") or ".").resolve())
    project_root = str(Path(payload.get("project_root") or root).resolve())
    out_base = payload.get("out_base") or "output/patches_received"
    out_file = payload.get("out_file") or str(Path(out_base) / "engine.out.json")
    return Norm(root=root, project_root=project_root, out_base=out_base, out_file=out_file)


def _get_records(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(meta, list):
        return meta
    return (
        meta.get("records")
        or (meta.get("result") or {}).get("records")
        or meta.get("items")
        or []
    )


def _messages_from_build(res: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Returns: (batches, messages_flattened_for_logging)

    Builder may return:
      - {"messages": [{"role":...,"content":...}, ...], "ids":[...], "batch":[...]}
      - or {"messages": {"system":"...","user":"..."}}
    """
    msgs = res.get("messages") or {}
    if isinstance(msgs, list):
        batches = [{"messages": msgs, "id": "pipeline-batch-0"}]
        return batches, msgs
    if isinstance(msgs, dict):
        system = msgs.get("system", "")
        user = msgs.get("user", "")
        m = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return [{"messages": m, "id": "pipeline-batch-0"}], m
    return [], []


def _parse_llm_items(raw: str) -> List[Dict[str, Any]]:
    """
    Parse a single LLM raw text into a list of item dicts.
    Intentionally domain-neutral.
    """
    try:
        from v2.backend.core.prompt_pipeline.llm.response_parser import parse_json_response  # type: ignore
    except Exception:
        parse_json_response = None

    if parse_json_response:
        try:
            obj = parse_json_response(raw) or {}
            rows = obj.get("items") or obj.get("results") or []
            return [r for r in rows if isinstance(r, dict)]
        except Exception:
            pass

    try:
        import json as _json
        obj = _json.loads(raw)
        rows = obj.get("items") or obj.get("results") or obj
        if isinstance(rows, dict):
            rows = rows.get("items") or rows.get("results") or []
        return [r for r in (rows or []) if isinstance(r, dict)]
    except Exception:
        return []


def _make_per_item_batches(
    project_root: str, items: List[Dict[str, Any]], ask_spec: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    Domain-neutral per-item fallback:
      - For each item, delegate to `prompts.build.v1` for that single item.
    """
    batches: List[Dict[str, Any]] = []

    for it in items:
        single_payload = {
            "root": project_root,
            "project_root": project_root,
            "items": [it],
            "ask_spec": ask_spec or {},
        }
        try:
            arts = capability_run("prompts.build.v1", single_payload, {"phase": "BUILD.FALLBACK"})
            meta = getattr(arts[0], "meta", arts[0]) if arts else {}
            res = (meta.get("result") or meta or {})
            msgs = res.get("messages")
            if isinstance(msgs, list) and msgs:
                batches.append({"id": str(it.get("id") or it.get("relpath") or ""), "messages": msgs, "ask_spec": ask_spec or {}})
            elif isinstance(msgs, dict):
                system = msgs.get("system", "")
                user = msgs.get("user", "")
                m = [{"role": "system", "content": system}, {"role": "user", "content": user}]
                batches.append({"id": str(it.get("id") or it.get("relpath") or ""), "messages": m, "ask_spec": ask_spec or {}})
        except Exception:
            continue

    return batches


# --------------------------------- run ---------------------------------------


def run_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Engine entry point.
    Accepts a 'task' or raw dict payload. Returns summary dict with run_dir and counts.
    """
    task = task_like or {}
    payload: Dict[str, Any] = getattr(task, "payload", None) or task or {}
    payload.update(kwargs or {})

    print("[llm.engine.run.v1] engine ACTIVE (bundle logging + prompt inject via Spine)")
    norm = _normalize(payload)
    run_dir = Path(norm.out_base) / _now_token()
    _ensure_dir(run_dir)

    # --------------------------- PHASE: FETCH --------------------------------
    print("[PHASE] FETCH")
    records: List[Dict[str, Any]] = []
    fetch_meta: Dict[str, Any] = {}
    if payload.get("sqlalchemy_url") and payload.get("sqlalchemy_table"):
        fetch_arts = capability_run(
            "introspect.fetch.v1",
            {
                "sqlalchemy_url": payload["sqlalchemy_url"],
                "sqlalchemy_table": payload["sqlalchemy_table"],
                "status": payload.get("status"),
                "max_rows": payload.get("max_rows", 50),
            },
            {"phase": "FETCH"},
        )
        fetch_meta = getattr(fetch_arts[0], "meta", fetch_arts[0]) if fetch_arts else {}
        _write_json(run_dir / "fetch.meta.json", fetch_meta)
        records = _get_records(fetch_meta)
    print(f"[FETCH] records={len(records)}")

    # --------------------------- PHASE: ENRICH -------------------------------
    print("[PHASE] ENRICH")
    items_enriched: List[Dict[str, Any]] = []
    if isinstance(payload.get("items"), list) and payload["items"]:
        items_enriched = list(payload["items"])
    elif records:
        enr_arts = capability_run(
            "retriever.enrich.v1",
            {
                "root": norm.root,
                "project_root": norm.project_root,
                "records": records,
                "exclude_globs": payload.get("exclude_globs") or [],
                "segment_excludes": payload.get("segment_excludes") or [],
            },
            {"phase": "ENRICH"},
        )
        enr_meta = getattr(enr_arts[0], "meta", enr_arts[0]) if enr_arts else {}
        _write_json(run_dir / "enrich.meta.json", enr_meta)
        items_enriched = (enr_meta.get("items") or (enr_meta.get("result") or {}).get("items") or [])
    print(f"[ENRICH] items={len(items_enriched)}")

    # ------------------------ PHASE: CONTEXT.BUILD ---------------------------
    print("[PHASE] CONTEXT.BUILD")
    items_for_build = list(items_enriched)
    if items_enriched:
        ctx_arts = capability_run(
            "context.build",
            {"items": items_enriched, "options": payload.get("context_options") or {}},
            {"phase": "CONTEXT.BUILD"},
        )
        ctx_meta = getattr(ctx_arts[0], "meta", ctx_arts[0]) if ctx_arts else {}
        _write_json(run_dir / "context.meta.json", ctx_meta)
        ctx_items = (ctx_meta.get("items") or (ctx_meta.get("result") or {}).get("items") or [])
        # Merge contexts by id
        ctx_by_id = {str(i.get("id")): (i.get("context") or {}) for i in ctx_items if isinstance(i, dict)}
        merged: List[Dict[str, Any]] = []
        for it in items_enriched:
            iid = str(it.get("id"))
            merged.append({**it, "context": {**(it.get("context") or {}), **ctx_by_id.get(iid, {})}})
        items_for_build = merged

    # --------------------------- PHASE: BUILD --------------------------------
    print("[PHASE] BUILD")
    build_payload = {
        "root": norm.root,
        "project_root": norm.project_root,
        "items": items_for_build,
        "provider": payload.get("provider"),
        "model": payload.get("model"),
        "ask_spec": payload.get("ask_spec") or {},
    }
    build_arts = capability_run("prompts.build.v1", build_payload, {"phase": "BUILD"})
    build_meta = getattr(build_arts[0], "meta", build_arts[0]) if build_arts else {}
    res = (build_meta.get("result") or build_meta or {})
    _write_json(run_dir / "build.result.json", res)

    messages_batch, msgs_log = _messages_from_build(res)

    # ------------------------- PHASE: BUNDLE.INJECT --------------------------
    # Ensure the run goes through code_bundles via Spine (no direct imports).
    print("[PHASE] BUNDLE.INJECT")
    try:
        capability_run(
            "packager.bundle.inject_prompt.v1",
            {
                "root": norm.root,
                "project_root": norm.project_root,
                "run_dir": str(run_dir),
                "messages": msgs_log,
                "batches": messages_batch,
                "provider": payload.get("provider"),
                "model": payload.get("model"),
                "ask_spec": payload.get("ask_spec") or {},
            },
            {"phase": "BUNDLE.INJECT"},
        )
    except Exception as e:
        # Do not fail the entire run on bundle logging issues; record and continue.
        _write_json(run_dir / "bundle.inject.error.json", {"error": str(e)})

    # ---------------------------- PHASE: LLM ---------------------------------
    print("[PHASE] LLM")
    _write_json(run_dir / "llm.input.json", {
        "has_batches": bool(messages_batch),
        "top_ids_len": len(res.get("ids", [])) if isinstance(res.get("ids"), list) else 0,
    })

    llm_results: List[Dict[str, Any]] = []
    if messages_batch:
        llm_arts = capability_run(
            "llm.complete_batches.v1",
            {
                "run_dir": str(run_dir),
                "root": norm.root,
                "provider": payload.get("provider"),
                "model": payload.get("model"),
                "batches": [
                    {"messages": b["messages"], "ask_spec": payload.get("ask_spec") or {}, "id": b.get("id", "pipeline-batch-0")}
                    for b in messages_batch
                ],
                "ask_spec": payload.get("ask_spec") or {},
            },
            {"phase": "LLM"},
        )
        llm_meta = getattr(llm_arts[0], "meta", llm_arts[0]) if llm_arts else {}
        llm_results = list(llm_meta.get("results") or [])
        _write_json(run_dir / "llm.results.json", llm_results)

    # --------------------------- PHASE: SANITIZE ------------------------------
    print("[PHASE] SANITIZE")
    parsed_items: List[Dict[str, Any]] = []
    for r in llm_results:
        raw = (r or {}).get("raw", "") or (r or {}).get("text", "")
        if not raw:
            continue
        parsed_items.extend(_parse_llm_items(raw))

    if not parsed_items and items_enriched:
        print("[LLM.FALLBACK] parsed_items=0; retrying with per-item batches")
        per_item_batches = _make_per_item_batches(norm.project_root, items_for_build, payload.get("ask_spec") or {})
        _write_json(run_dir / "llm.fallback.batches.json", per_item_batches)
        fb_arts = capability_run(
            "llm.complete_batches.v1",
            {
                "run_dir": str(run_dir),
                "root": norm.root,
                "provider": payload.get("provider"),
                "model": payload.get("model"),
                "batches": per_item_batches,
                "ask_spec": payload.get("ask_spec") or {},
            },
            {"phase": "LLM.FALLBACK"},
        )
        fb_meta = getattr(fb_arts[0], "meta", fb_arts[0]) if fb_arts else {}
        fb_results = list(fb_meta.get("results") or [])
        _write_json(run_dir / "llm.fallback.results.json", fb_results)
        for r in fb_results:
            raw = (r or {}).get("raw", "") or (r or {}).get("text", "")
            if not raw:
                continue
            parsed_items.extend(_parse_llm_items(raw))

    sanitized_arts = capability_run(
        "sanitize.v1",
        {"run_dir": str(run_dir), "project_root": norm.root, "prepared_batch": res.get("batch") or items_for_build or [], "items": parsed_items},
        {"phase": "SANITIZE"},
    )
    sanitize_meta = getattr(sanitized_arts[0], "meta", sanitized_arts[0]) if sanitized_arts else {}
    _write_json(run_dir / "sanitize.meta.json", sanitize_meta)
    items_after_sanitize = (sanitize_meta.get("result") or sanitize_meta or [])
    if isinstance(items_after_sanitize, dict):
        items_after_sanitize = items_after_sanitize.get("items") or items_after_sanitize.get("result") or []
    print(f"[SANITIZE] items={len(items_after_sanitize)}")

    # ---------------------------- PHASE: VERIFY ------------------------------
    print("[PHASE] VERIFY")
    verify_arts = capability_run(
        "verify.v1",
        {"run_dir": str(run_dir), "project_root": norm.root, "items": items_after_sanitize},
        {"phase": "VERIFY"},
    )
    verify_meta = getattr(verify_arts[0], "meta", verify_arts[0]) if verify_arts else {}
    _write_json(run_dir / "verify.meta.json", verify_meta)
    ok_items = (verify_meta.get("ok_items") or (verify_meta.get("result") or {}).get("ok_items") or items_after_sanitize)
    if not isinstance(ok_items, list):
        ok_items = []
    print(f"[VERIFY] ok_items={len(ok_items)}")

    # ------------------------- PHASE: PATCH.APPLY_FILES ----------------------
    print("[PHASE] PATCH.APPLY_FILES")
    apply_payload = {
        "run_dir": str(run_dir),
        "out_base": norm.out_base,
        "items": ok_items,
        "prepared_batch": res.get("batch") or items_for_build or [],
        "raw_prompts": res.get("messages") or {},
        "raw_responses": llm_results,
        "sqlalchemy_url": payload.get("sqlalchemy_url"),
        "sqlalchemy_table": payload.get("sqlalchemy_table"),
        "strip_prefix": payload.get("strip_prefix", ""),
        "mirror_to": payload.get("patch_target_root", ""),
        "patch_seed_strategy": payload.get("patch_seed_strategy", "once"),
    }
    apply_arts = capability_run("patch.apply_files.v1", apply_payload, {"phase": "PATCH.APPLY_FILES"})
    apply_meta = getattr(apply_arts[0], "meta", apply_arts[0]) if apply_arts else {}
    _write_json(run_dir / "apply.meta.json", apply_meta)

    # ------------------------------ FINALIZE ---------------------------------
    summary = {
        "run_dir": str(run_dir.resolve()),
        "apply_meta": apply_meta,
        "counts": {
            "built_messages": len(msgs_log),
            "built_batch": len(res.get("batch") or []),
            "sanitized": len(items_after_sanitize),
            "verified": len(ok_items),
        },
    }
    _write_json(Path(norm.out_file), summary)
    return {"run_dir": summary["run_dir"], "counts": summary["counts"]}






