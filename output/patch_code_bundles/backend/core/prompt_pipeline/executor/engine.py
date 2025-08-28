# v2/backend/core/prompt_pipeline/executor/engine.py
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Generic capability dispatcher (domain-agnostic)
from v2.backend.core.spine.loader import capability_run

# Universal patch planning + diff compiler
from v2.backend.core.patch_engine.plan import (
    PatchPlan,
    ReplaceRange,
    InsertAt,
    DeleteRange,
    AddFile,
    DeleteFile,
)
from v2.backend.core.patch_engine.ops_compile import ops_to_unified_diff


@dataclass
class EngineNormalize:
    root: str
    project_root: str
    out_base: str
    out_file: str


def _extract_payload(task_like: Any) -> Dict[str, Any]:
    """
    Accept either a dict or a Task-like object with a .payload (or .data/.meta) dict.
    """
    if isinstance(task_like, dict):
        return task_like
    for key in ("payload", "data", "meta"):
        v = getattr(task_like, key, None)
        if isinstance(v, dict):
            return v
    # Last resort: try mapping-ish objects
    try:
        return dict(task_like)  # type: ignore[arg-type]
    except Exception:
        raise TypeError("engine.run_v1 expected a dict or Task-like object with a .payload dict")


def _normalize_payload(payload: Dict[str, Any]) -> EngineNormalize:
    """
    Normalize and validate required paths (generic).
    """
    def _req_str(d: Dict[str, Any], k: str) -> str:
        v = d.get(k)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"payload must include '{k}'")
        return v

    root = _req_str(payload, "root")
    out_file = _req_str(payload, "out_file")
    out_base = str(payload.get("out_base") or Path(out_file).parent.as_posix())
    project_root = str(payload.get("project_root") or root)

    # Normalize to absolute paths
    root = str(Path(root).expanduser().resolve())
    project_root = str(Path(project_root).expanduser().resolve())
    out_file = str(Path(out_file).expanduser().resolve())

    # Ensure out_base folder exists (if provided as relative, base it on CWD)
    out_dir = Path(out_base)
    if not out_dir.is_absolute():
        out_dir = Path.cwd() / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    return EngineNormalize(root=root, project_root=project_root, out_base=out_base, out_file=out_file)


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _ensure_run_dir(out_base: str) -> Path:
    """
    Create a per-run directory under out_base with a timestamp. Generic naming.
    """
    base = Path(out_base)
    if not base.is_absolute():
        base = Path.cwd() / base
    run_dir = base / _timestamp()
    (run_dir / "patches").mkdir(parents=True, exist_ok=True)
    return run_dir


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def run_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Generic pipeline runner (domain-agnostic).
    """
    _ = (context, kwargs)  # intentionally unused; maintains backward-compat signature

    payload = _extract_payload(task_like)

    print("[llm.engine.run.v1] hardened engine ACTIVE (bundle attach enabled)")

    # Normalize
    norm = _normalize_payload(payload)
    print("[ENGINE] launching with keys:", ["out_base", "out_file", "project_root", "root"])
    print(f"[ENGINE DEBUG] file= {Path(__file__).resolve()} has_norm= True keys= ['out_base', 'out_file', 'project_root', 'root']")
    print(f"[ENGINE NORMALIZED] root= {norm.root.replace('\\', '/')} out_file= {norm.out_file} out_base= {norm.out_base}")

    # ---- FETCH ----
    print("[PHASE] FETCH")

    # Prefer explicit records from payload; otherwise try DB-backed fetch
    records = list((payload or {}).get("records") or [])

    if not records and (payload.get("sqlalchemy_url") and payload.get("sqlalchemy_table")):
        fetch_arts = capability_run("introspect.fetch.v1", {
            "sqlalchemy_url": payload["sqlalchemy_url"],
            "sqlalchemy_table": payload["sqlalchemy_table"],
            # Optional filters (pass through if provided in vars)
            "status": payload.get("status"),
            "status_any": payload.get("status_any"),
            "exclude_globs": payload.get("exclude_globs") or [],
            "segment_excludes": payload.get("segment_excludes") or [],
            "max_rows": int(payload.get("max_rows", 200)),
        })
        fetch_meta = getattr(fetch_arts[0], "meta", fetch_arts[0])
        items = (fetch_meta.get("result", {}) or {}).get("items", []) or []

        # Normalize DB “items” → builder “records”
        records = [{
            "id": it.get(
                "id") or f"{(it.get('file') or it.get('filepath') or '')}:{int(it.get('line') or it.get('lineno') or 1)}",
            "filepath": it.get("file") or it.get("filepath") or "",
            "lineno": int(it.get("line") or it.get("lineno") or 1),
            "symbol_type": it.get("filetype") or it.get("symbol_type") or "unknown",
            "description": it.get("description") or "",
        } for it in items if (it.get("file") or it.get("filepath"))]

    # ---- BUILD ----
    print("[PHASE] BUILD")
    build_payload = {
        "root": norm.root,
        "project_root": norm.project_root,
        "records": records,  # <-- key bit; builder can now work
        # pass through anything else from payload except engine outputs
        **{k: v for k, v in (payload or {}).items() if k not in ("out_file", "out_base")}
    }
    build_arts = capability_run("prompts.build.v1", build_payload)
    build_meta = getattr(build_arts[0], "meta", build_arts[0])
    res = build_meta.get("result", {}) or {}
    print("[BUILD] meta keys=", list(build_meta.keys()))
    print("[BUILD] result keys=", list(res.keys()))
    print("[BUILD] counts: messages=", len(res.get("messages", [])),
          " ids=", len(res.get("ids", [])),
          " batch=", len(res.get("batch", [])))

    # PHASE: PREPARE RUN DIR
    print("[PHASE] PREPARE RUN DIR]")
    run_dir = _ensure_run_dir(norm.out_base)

    # Optional bundle phases (no-ops if providers are absent)
    print("[PHASE] BUNDLE.MAKE")
    try:
        capability_run("bundle.make.v1", {"run_dir": str(run_dir), "root": norm.root})
    except Exception:
        pass

    print("[PHASE] BUNDLE.INJECT")
    try:
        capability_run("bundle.inject.v1", {"run_dir": str(run_dir), "root": norm.root})
    except Exception:
        pass

    # PHASE: LLM (optional; depends on builder output)
    print("[PHASE] LLM")
    llm_payload = {
        "meta_keys": ["provider", "model", "ask_spec", "batches", "messages", "ids", "bundle"],
        "has_batches": bool(res.get("batch")),
        "top_ids_len": len(res.get("ids", [])),
        "ctx_keys": ["state"],
        "ctx_build_keys": ["items"],
        "ctx_ids_len": 0,
    }
    print("[LLM.provider.input]", json.dumps(llm_payload, ensure_ascii=False))
    _write_json(Path(norm.out_file).parent / "llm.input.json", llm_payload)
    try:
        capability_run("llm.complete_batches.v1", {
            "run_dir": str(run_dir),
            "root": norm.root,
            "messages": res.get("messages") or [],
            "batches": res.get("batch") or [],
            "ids": res.get("ids") or [],
        })
    except Exception:
        # Fine if your pipeline doesn't use this
        pass

    # PHASE: SANITIZE (generic)
    print("[PHASE] SANITIZE")
    sanitized_arts = capability_run("sanitize.v1", {
        "run_dir": str(run_dir),
        "project_root": norm.root,
        "prepared_batch": res.get("batch") or [],
        "items": payload.get("items") or [],
    })
    sanitize_meta = getattr(sanitized_arts[0], "meta", sanitized_arts[0])
    items_after_sanitize = sanitize_meta.get("result", []) or []
    print(f"[SANITIZE] items={len(items_after_sanitize)}")

    # PHASE: VERIFY (generic)
    print("[PHASE] VERIFY")
    verified_arts = capability_run("verify.v1", {
        "items": items_after_sanitize,
        "policy": os.environ.get("VERIFY_POLICY", "lenient"),
    })
    verify_meta = getattr(verified_arts[0], "meta", verified_arts[0])
    ok_items = (verify_meta.get("result", {}) or {}).get("items", items_after_sanitize) or []
    print(f"[VERIFY] ok_items={len(ok_items)}")

    # PHASE: PATCH.PLAN (generic)
    print("[PHASE] PATCH.PLAN")
    plan_arts = capability_run("patch.plan.v1", {
        "items": ok_items,
        "project_root": norm.root,
        "width": int(os.environ.get("FORMAT_WIDTH", "72")),
    })
    plan_meta = getattr(plan_arts[0], "meta", plan_arts[0])
    plan_ops_raw = (plan_meta.get("plan", {}) or {}).get("ops", []) or []

    _OP_CLASS_MAP = {
        "ReplaceRange": ReplaceRange,
        "InsertAt": InsertAt,
        "DeleteRange": DeleteRange,
        "AddFile": AddFile,
        "DeleteFile": DeleteFile,
    }
    ops: List[Any] = []
    for raw in plan_ops_raw:
        raw = dict(raw)
        op_type = raw.pop("op")
        ops.append(_OP_CLASS_MAP[op_type](**raw))
    plan = PatchPlan(ops=ops)

    # Compile to unified diff and write patch file
    diff_text = ops_to_unified_diff(plan, workspace=Path(norm.root))
    patch_dir = run_dir / "patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_path = patch_dir / "000_plan.patch"
    patch_path.write_text(diff_text, encoding="utf-8")

    # PHASE: PATCH.APPLY_FILES (generic)
    print("[PHASE] PATCH.APPLY_FILES")
    apply_arts = capability_run("patch.apply_files.v1", {
        "run_dir": str(run_dir),
        "patches": [str(patch_path)],
        "strip_prefix": payload.get("strip_prefix", ""),
        "mirror_to": payload.get("patch_target_root", ""),  # optional mirror
    })
    # You can inspect apply_arts[0].meta if you need details

    print("[FINALIZE] strip_prefix=" + str(payload.get("strip_prefix", "")))
    if payload.get("patch_target_root"):
        print(f"[FINALIZE] mirrored sandbox_applied → {payload.get('patch_target_root')}")

    return {
        "run_dir": str(run_dir),
        "patch_file": str(patch_path),
        "counts": {
            "built_messages": len(res.get("messages", [])),
            "built_batch": len(res.get("batch", [])),
            "sanitized": len(items_after_sanitize),
            "verified": len(ok_items),
        },
    }


# Backward-compatible alias (some callers import this name)
engine_run_v1 = run_v1


