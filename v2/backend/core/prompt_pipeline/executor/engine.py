# File: v2/backend/core/prompt_pipeline/executor/engine.py
"""
Docstrings/patch pipeline engine (capability-routed).

Phases:
  FETCH -> ENRICH -> BUILD -> (BUNDLE.MAKE/INJECT logs only) -> LLM
  -> SANITIZE -> VERIFY -> PATCH.APPLY_FILES -> FINALIZE

All cross-module work is performed via Spine capabilities.
This file wires phases together and persists run artifacts/output.
"""

from __future__ import annotations

import json
import os
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
        pass


def _now_token() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _normalize(payload: Dict[str, Any]) -> Norm:
    root = str((Path(payload.get("root") or payload.get("project_root") or Path.cwd())).resolve())
    project_root = str((Path(payload.get("project_root") or root)).resolve())
    out_base = payload.get("out_base") or "output/patches_received"
    out_file = payload.get("out_file") or str(Path(out_base) / "engine.out.json")
    return Norm(root=root, project_root=project_root, out_base=out_base, out_file=out_file)


def _get_records(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    # Accept {records:[...]} or {result:{records:[...]}} or direct list
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
        batches = [{"messages": msgs, "id": "docstrings-batch-0"}]
        return batches, msgs
    if isinstance(msgs, dict):
        system = msgs.get("system", "")
        user = msgs.get("user", "")
        m = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return [{"messages": m, "id": "docstrings-batch-0"}], m
    # fallback: no messages
    return [], []


def _parse_llm_items(raw: str) -> List[Dict[str, Any]]:
    """
    Parse a single LLM raw text into [{id, docstring}, ...].
    Prefer the pipeline's response_parser if available, then fall back to JSON.
    """
    try:
        from v2.backend.core.prompt_pipeline.llm.response_parser import parse_json_response  # type: ignore
    except Exception:
        parse_json_response = None

    if parse_json_response:
        try:
            obj = parse_json_response(raw) or {}
            rows = obj.get("items") or obj.get("results") or []
            out = []
            for it in rows:
                if isinstance(it, dict) and "id" in it and "docstring" in it:
                    out.append({"id": str(it["id"]), "docstring": str(it["docstring"])})
            return out
        except Exception:
            pass

    # Fallback: attempt direct JSON load
    try:
        obj = json.loads(raw)
        rows = obj.get("items") or obj.get("results") or obj
        if isinstance(rows, dict):
            rows = rows.get("items") or rows.get("results") or []
        out = []
        for it in rows or []:
            if isinstance(it, dict) and "id" in it and "docstring" in it:
                out.append({"id": str(it["id"]), "docstring": str(it["docstring"])})
        return out
    except Exception:
        return []


def _make_per_item_batches(project_root: str, items: List[Dict[str, Any]], ask_spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build one LLM batch per target item as a fallback when aggregate parsing fails.
    Attempts to use your docstrings.prompt_builder if present, otherwise makes a minimal prompt.
    """
    batches: List[Dict[str, Any]] = []
    sys_text = "You are a precise Python documentation assistant. Reply ONLY with JSON like: [{\"id\":\"\",\"docstring\":\"\"}]. No prose."
    have_pb = False
    try:
        from v2.backend.core.docstrings.prompt_builder import build_system_prompt, build_user_prompt  # type: ignore
        sys_text = build_system_prompt()
        have_pb = True
    except Exception:
        have_pb = False

    pr = Path(project_root)

    # Optional: small code context to help the model
    def _load_ctx(relpath: str, target_lineno: int, half: int = 20) -> str:
        try:
            src = (pr / relpath).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
        lines = src.splitlines()
        i0 = max(0, int(target_lineno) - 1 - half)
        i1 = min(len(lines), int(target_lineno) - 1 + half + 1)
        return "\n".join(lines[i0:i1])

    for it in items:
        rel = (it.get("relpath") or it.get("filepath") or it.get("file") or "").replace("\\", "/")
        target_lineno = int(it.get("target_lineno") or it.get("lineno") or 1)
        mode = "rewrite" if it.get("has_docstring") else "create"
        signature = it.get("signature") or "module"
        desc = (it.get("description") or "").strip()
        ctx = _load_ctx(rel, target_lineno) if rel else ""

        if have_pb:
            user_text = build_user_prompt([{
                "id": it.get("id") or f"{rel}#{target_lineno}",
                "mode": mode,
                "signature": signature,
                "has_docstring": bool(it.get("has_docstring")),
                "description": desc,
                "context_code": ctx,
            }])
        else:
            # Minimalistic single-item prompt
            lines: List[str] = [
                "Return ONLY JSON list like this (no prose):",
                '[{"id": "ID", "docstring": "TEXT"}]',
                f"id: {it.get('id') or f'{rel}#{target_lineno}'}",
                f"mode: {mode}",
                f"signature: {signature}",
            ]
            if desc:
                lines.append(f"description: {desc}")
            if ctx:
                lines.append("context_code:\n```python")
                lines.append(ctx)
                lines.append("```")
            user_text = "\n".join(lines)

        batches.append({
            "id": str(it.get("id") or f"{rel}#{target_lineno}"),
            "messages": [
                {"role": "system", "content": sys_text},
                {"role": "user", "content": user_text},
            ],
            "ask_spec": ask_spec or {},
        })
    return batches


# --------------------------------- run ---------------------------------------

def run_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Engine entry point. Accepts a 'task' or raw dict payload.
    Returns summary dict with run_dir and counts.
    """
    task = task_like or {}
    payload: Dict[str, Any] = getattr(task, "payload", None) or task or {}
    payload.update(kwargs or {})

    print("[llm.engine.run.v1] hardened engine ACTIVE (bundle attach enabled)")
    print("[ENGINE] launching with keys:", list(payload.keys()))
    norm = _normalize(payload)
    print("[ENGINE DEBUG] file=", __file__, "has_norm=", True, "keys=", list(payload.keys()))
    print("[ENGINE NORMALIZED]",
          "root=", norm.root,
          "out_file=", norm.out_file,
          "out_base=", norm.out_base)

    run_dir = Path(norm.out_base) / _now_token()
    _ensure_dir(run_dir)

    # --------------------------- PHASE: FETCH --------------------------------
    print("[PHASE] FETCH")
    records: List[Dict[str, Any]] = []
    fetch_meta: Dict[str, Any] = {}
    if payload.get("sqlalchemy_url") and payload.get("sqlalchemy_table"):
        fetch_arts = capability_run("introspect.fetch.v1", {
            "sqlalchemy_url": payload["sqlalchemy_url"],
            "sqlalchemy_table": payload["sqlalchemy_table"],
            "status": payload.get("status"),
            "max_rows": payload.get("max_rows", 50),
        }, {"phase": "FETCH"})
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
        enr_arts = capability_run("retriever.enrich.v1", {
            "root": norm.root,
            "project_root": norm.project_root,
            "records": records,
            "exclude_globs": payload.get("exclude_globs") or [],
            "segment_excludes": payload.get("segment_excludes") or [],
        }, {"phase": "ENRICH"})
        enr_meta = getattr(enr_arts[0], "meta", enr_arts[0]) if enr_arts else {}
        _write_json(run_dir / "enrich.meta.json", enr_meta)
        items_enriched = (enr_meta.get("items")
                          or (enr_meta.get("result") or {}).get("items")
                          or [])
    print(f"[ENRICH] items={len(items_enriched)}")

    # --------------------------- PHASE: BUILD --------------------------------
    print("[PHASE] BUILD")
    build_payload = {
        "root": norm.root,
        "project_root": norm.project_root,
        "items": items_enriched,
        # pass through core knobs
        "provider": payload.get("provider"),
        "model": payload.get("model"),
        "ask_spec": payload.get("ask_spec") or {},
    }
    build_arts = capability_run("prompts.build.v1", build_payload, {"phase": "BUILD"})
    build_meta = getattr(build_arts[0], "meta", build_arts[0]) if build_arts else {}
    res = (build_meta.get("result") or build_meta or {})  # tolerate provider styles
    _write_json(run_dir / "build.result.json", res)
    messages_batch, msgs_log = _messages_from_build(res)

    # ---------------------- PHASE: PREPARE RUN DIR ---------------------------
    print("[PHASE] PREPARE RUN DIR]")
    # already wrote build.result.json

    # ------------------------- PHASE: BUNDLE.* (logs) ------------------------
    print("[PHASE] BUNDLE.MAKE")
    print("[PHASE] BUNDLE.INJECT")

    # ---------------------------- PHASE: LLM ---------------------------------
    print("[PHASE] LLM")
    llm_input_dbg = {
        "meta_keys": ["provider", "model", "ask_spec", "batches", "messages", "ids", "bundle"],
        "has_batches": bool(messages_batch),
        "top_ids_len": len(res.get("ids", [])) if isinstance(res.get("ids"), list) else 0,
        "ctx_keys": ["state"],
        "ctx_build_keys": ["items"],
        "ctx_ids_len": 0,
    }
    print("[LLM.provider.input]", json.dumps(llm_input_dbg, ensure_ascii=False))
    _write_json(run_dir / "llm.input.json", llm_input_dbg)

    llm_results: List[Dict[str, Any]] = []
    if messages_batch:
        llm_arts = capability_run("llm.complete_batches.v1", {
            "run_dir": str(run_dir),
            "root": norm.root,
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "batches": [
                {
                    "messages": b["messages"],
                    "ask_spec": payload.get("ask_spec") or {},
                    "id": b.get("id", "docstrings-batch-0"),
                } for b in messages_batch
            ],
            "ask_spec": payload.get("ask_spec") or {},
            "bundle": payload.get("bundle") or {},
        }, {"phase": "LLM"})
        llm_meta = getattr(llm_arts[0], "meta", llm_arts[0]) if llm_arts else {}
        llm_results = list(llm_meta.get("results") or [])
    _write_json(run_dir / "llm.results.json", llm_results)

    # --------------------------- PHASE: SANITIZE (pass 1) --------------------
    print("[PHASE] SANITIZE")
    parsed_items: List[Dict[str, Any]] = []
    for r in llm_results:
        raw = (r or {}).get("raw", "") or (r or {}).get("text", "")
        if not raw:
            continue
        parsed_items.extend(_parse_llm_items(raw))

    # If we still have nothing, do a per-item fallback LLM pass.
    if not parsed_items and items_enriched:
        print("[LLM.FALLBACK] parsed_items=0; retrying with per-item batches")
        per_item_batches = _make_per_item_batches(norm.project_root, items_enriched, payload.get("ask_spec") or {})
        _write_json(run_dir / "llm.fallback.batches.json", per_item_batches)
        fb_arts = capability_run("llm.complete_batches.v1", {
            "run_dir": str(run_dir),
            "root": norm.root,
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "batches": per_item_batches,
            "ask_spec": payload.get("ask_spec") or {},
        }, {"phase": "LLM.FALLBACK"})
        fb_meta = getattr(fb_arts[0], "meta", fb_arts[0]) if fb_arts else {}
        fb_results = list(fb_meta.get("results") or [])
        _write_json(run_dir / "llm.fallback.results.json", fb_results)
        for r in fb_results:
            raw = (r or {}).get("raw", "") or (r or {}).get("text", "")
            if not raw:
                continue
            parsed_items.extend(_parse_llm_items(raw))

    sanitized_arts = capability_run("sanitize.v1", {
        "run_dir": str(run_dir),
        "project_root": norm.root,
        "prepared_batch": res.get("batch") or items_enriched or [],
        "items": parsed_items,
    }, {"phase": "SANITIZE"})
    sanitize_meta = getattr(sanitized_arts[0], "meta", sanitized_arts[0]) if sanitized_arts else {}
    _write_json(run_dir / "sanitize.meta.json", sanitize_meta)

    items_after_sanitize = (sanitize_meta.get("result") or sanitize_meta or [])
    if isinstance(items_after_sanitize, dict):
        items_after_sanitize = items_after_sanitize.get("items") or items_after_sanitize.get("result") or []
    print(f"[SANITIZE] items={len(items_after_sanitize)}")

    # ---------------------------- PHASE: VERIFY ------------------------------
    print("[PHASE] VERIFY")
    verify_arts = capability_run("verify.v1", {
        "run_dir": str(run_dir),
        "project_root": norm.root,
        "items": items_after_sanitize,
    }, {"phase": "VERIFY"})
    verify_meta = getattr(verify_arts[0], "meta", verify_arts[0]) if verify_arts else {}
    _write_json(run_dir / "verify.meta.json", verify_meta)
    ok_items = (verify_meta.get("ok_items")
                or (verify_meta.get("result") or {}).get("ok_items")
                or items_after_sanitize)
    if not isinstance(ok_items, list):
        ok_items = []
    print(f"[VERIFY] ok_items={len(ok_items)}")

    # ------------------------- PHASE: PATCH.APPLY_FILES ----------------------
    print("[PHASE] PATCH.APPLY_FILES")
    apply_payload = {
        "run_dir": str(run_dir),
        "out_base": norm.out_base,
        "items": ok_items,
        "prepared_batch": res.get("batch") or items_enriched or [],
        "raw_prompts": res.get("messages") or {},
        "raw_responses": llm_results,
        # Required by patch.run.v1 path when passing 'items'
        "sqlalchemy_url": payload.get("sqlalchemy_url"),
        "sqlalchemy_table": payload.get("sqlalchemy_table"),
        # Options
        "strip_prefix": payload.get("strip_prefix", ""),
        "mirror_to": payload.get("patch_target_root", ""),
        "patch_seed_strategy": payload.get("patch_seed_strategy", "once"),
    }
    apply_arts = capability_run("patch.apply_files.v1", apply_payload, {"phase": "PATCH.APPLY_FILES"})
    apply_meta = getattr(apply_arts[0], "meta", apply_arts[0]) if apply_arts else {}
    _write_json(run_dir / "apply.meta.json", apply_meta)

    # ------------------------------ FINALIZE ---------------------------------
    print("[FINALIZE] strip_prefix=", payload.get("strip_prefix", ""))
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
    return {
        "run_dir": summary["run_dir"],
        "counts": summary["counts"],
    }




