# v2/backend/core/prompt_pipeline/executor/engine.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import os, json, hashlib
from pathlib import Path
from datetime import datetime

from v2.backend.core.spine.contracts import Artifact

# fetch + build + parse/sanitize/verify
from v2.backend.core.introspect.providers import fetch_v1 as introspect_fetch_v1
from v2.backend.core.docstrings.providers import build_prompts_v1 as doc_prompts_build_v1
from v2.backend.core.prompt_pipeline.executor.providers import unpack_results_v1 as results_unpack_v1
from v2.backend.core.prompt_pipeline.llm.response_parser import parse_json_response
from v2.backend.core.docstrings.providers import sanitize_outputs_v1 as docstrings_sanitize_v1, verify_batch_v1 as docstrings_verify_v1

# LLM + Patch
from v2.backend.core.prompt_pipeline.llm.providers import complete_batches_v1 as llm_complete_batches_v1
from v2.backend.core.patch_engine.providers import run_v1 as patch_run_v1

# Code Bundle providers
from v2.backend.core.spine.providers.packager_bundle_make import run_v1 as bundle_make_v1
from v2.backend.core.spine.providers.packager_bundle_inject_prompt import run_v1 as bundle_inject_prompt_v1
from v2.backend.core.spine.providers.utils_code_index import run_v1 as utils_code_index_v1

# Run dir utils
from v2.backend.core.utils.io.run_dir import RunDirs


class _TaskShim:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload; self.envelope = {}; self.payload_schema = {}
    def __getitem__(self, k): return self.payload[k]
    def get(self, k, d=None): return self.payload.get(k, d)


def _ok(meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri="spine://result/llm.engine.run.v1", sha256="", meta=meta)]

def _ng(code: str, message: str, *, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [Artifact(kind="Problem", uri="spine://problem/llm.engine.run.v1", sha256="", meta={
        "problem": {"code": code, "message": message, "retryable": False, "details": details or {}}
    })]

def _as_bool(x: Any, default=False) -> bool:
    if isinstance(x, bool): return x
    if isinstance(x, str):
        s = x.strip().lower()
        if s in {"1","true","yes","on"}: return True
        if s in {"0","false","no","off"}: return False
    return default

def _ensure_items(obj: Any) -> List[Dict[str, Any]]:
    if isinstance(obj, list): return [dict(it) for it in obj if isinstance(it, dict)]
    return []

def _call_provider(fn, payload: Dict[str, Any], context: Dict[str, Any]) -> Any:
    t = _TaskShim(payload)
    try: return fn(t, context or {})
    except TypeError: return fn(t)

def _first_artifact(res: Any) -> Tuple[Optional[Artifact], Dict[str, Any]]:
    if isinstance(res, list) and res:
        a = res[0]
        if isinstance(a, Artifact): return a, dict(a.meta or {})
        if isinstance(a, dict): return Artifact(kind="Result", uri="spine://shim", sha256="", meta=a), dict(a)
        return None, {}
    if isinstance(res, dict):
        return Artifact(kind="Result", uri="spine://shim", sha256="", meta=res), dict(res)
    return None, {}

def _meta_pick(meta: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in meta and meta[k] is not None: return meta[k]
    return default


def _write_manifest_monolith(manifest_path: Path, items: List[Dict[str, Any]]) -> int:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as f:
        for it in items:
            rec = dict(it)
            if "record_type" not in rec: rec["record_type"] = "file"
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try: return manifest_path.stat().st_size
    except Exception: return 0

def _write_manifest_chunked(parts_dir: Path, parts_index: Path, items: List[Dict[str, Any]], split_bytes: int) -> Dict[str, Any]:
    parts_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for it in items:
        rec = dict(it)
        if "record_type" not in rec: rec["record_type"] = "file"
        lines.append(json.dumps(rec, ensure_ascii=False) + "\n")

    parts = []
    buf, buf_bytes, part_idx = [], 0, 0
    def flush():
        nonlocal buf, buf_bytes, part_idx
        if not buf: return
        name = f"{part_idx:02d}.txt"; p = parts_dir / name
        with p.open("w", encoding="utf-8") as f:
            for s in buf: f.write(s)
        parts.append({"name": name, "size": p.stat().st_size, "lines": len(buf)})
        part_idx += 1; buf = []; buf_bytes = 0

    for s in lines:
        sz = len(s.encode("utf-8"))
        if buf and buf_bytes + sz > split_bytes: flush()
        buf.append(s); buf_bytes += sz
    flush()

    idx = {"record_type": "parts_index", "dir": parts_dir.name, "total_parts": len(parts), "split_bytes": split_bytes, "parts": parts}
    parts_index.parent.mkdir(parents=True, exist_ok=True)
    parts_index.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    return idx


_ENGINE_DEPTH = 0


def run_v1(task: _TaskShim, context: Dict[str, Any]) -> List[Artifact]:
    print("[llm.engine.run.v1] hardened engine ACTIVE (bundle attach enabled)")

    p = dict(task.payload or {})
    ctx = dict(context or {})

    # Defaults
    try:
        from v2.backend.core.configuration.loader import get_llm
        llm_cfg = get_llm()
        _default_provider = str(getattr(llm_cfg, "provider", "") or getattr(llm_cfg, "name", "") or "")
        _default_model = str(getattr(llm_cfg, "model", "") or "")
    except Exception:
        _default_provider = ""; _default_model = ""
    try:
        from v2.backend.core.configuration.loader import get_db
        db_cfg = get_db()
        _default_sqlalchemy_url = str(getattr(db_cfg, "sqlalchemy_url", "") or "")
        _default_sqlalchemy_table = str(getattr(db_cfg, "sqlalchemy_table", "") or getattr(db_cfg, "table", "") or "")
    except Exception:
        _default_sqlalchemy_url = ""; _default_sqlalchemy_table = ""

    # Code bundle toggles
    cb = dict(p.get("code_bundle") or {})
    code_bundle_mode = (cb.get("mode") or "pipeline").strip().lower()
    include_manifest = _as_bool(cb.get("include_design_manifest"), True)
    chunk_manifest = (cb.get("chunk_manifest") or "auto").strip().lower()  # auto|always|never
    split_bytes = int(cb.get("split_bytes") or 300000)
    group_dirs = _as_bool(cb.get("group_dirs"), True)
    publish_github = _as_bool(cb.get("publish_github"), False)

    provider = str(p.get("provider") or _default_provider).strip()
    model = str(p.get("model") or _default_model).strip()
    sqlalchemy_url = str(p.get("sqlalchemy_url") or _default_sqlalchemy_url).strip()
    sqlalchemy_table = str(p.get("sqlalchemy_table") or _default_sqlalchemy_table or "introspection_index").strip()
    out_base = str(p.get("out_base") or "").strip()

    if not sqlalchemy_url or not sqlalchemy_table: return _ng("InvalidPayload","Missing sqlalchemy_url/sqlalchemy_table")
    if not provider or not model: return _ng("InvalidPayload","Missing provider/model")
    if not out_base: return _ng("InvalidPayload","Missing out_base")

    run_fetch = _as_bool(p.get("run_fetch_targets"), True)
    run_build = _as_bool(p.get("run_build_prompts"), True)
    run_llm = _as_bool(p.get("run_run_llm"), True)
    run_unpack = _as_bool(p.get("run_unpack"), True)
    run_sanitize = _as_bool(p.get("run_sanitize"), True)
    run_verify = _as_bool(p.get("run_verify"), True)
    run_save_patch = _as_bool(p.get("run_save_patch"), True)

    status_filter = str(p.get("status_filter") or "")
    max_rows = int(p.get("max_rows") or 200)
    exclude_globs = list(p.get("exclude_globs") or [])
    segment_excludes = list(p.get("segment_excludes") or [])
    ask_spec = dict(p.get("ask_spec") or {})

    global _ENGINE_DEPTH
    _ENGINE_DEPTH += 1
    try:
        # FETCH
        if run_fetch:
            res = _call_provider(introspect_fetch_v1, {
                "sqlalchemy_url": sqlalchemy_url, "sqlalchemy_table": sqlalchemy_table,
                "status": status_filter, "status_filter": status_filter,
                "max_rows": max_rows, "exclude_globs": exclude_globs, "segment_excludes": segment_excludes,
            }, ctx)
            art, meta = _first_artifact(res)
            if not art: return _ng("ProviderError","introspect.fetch.v1 returned nothing")
            if art.kind == "Problem":
                prob = (meta or {}).get("problem", {})
                return _ng(prob.get("code","ProviderProblem"), prob.get("message","fetch failed"), details=prob)
            items = _ensure_items(_meta_pick(meta, "items", "result", default=[]))
            if not items: return _ng("ValidationError","No valid targets found.")
        else:
            items = _ensure_items(p.get("items"))
            if not items: return _ng("InvalidPayload","items required when run_fetch_targets=false")

        # BUILD
        if run_build:
            res = _call_provider(doc_prompts_build_v1, {
                "records": items, "project_root": os.getcwd(),
                "context_half_window": int(p.get("context_half_window", 25)),
                "description_field": str(p.get("description_field", "description")),
            }, ctx)
            art, meta = _first_artifact(res)
            if not art: return _ng("ProviderError","docstrings.prompts.build.v1 returned nothing")
            if art.kind == "Problem":
                prob = (meta or {}).get("problem", {})
                return _ng(prob.get("code","ProviderProblem"), prob.get("message","build prompts failed"), details=prob)
            res_obj = _meta_pick(meta, "result", default={}) or {}
            messages = dict(res_obj.get("messages") or {})
            prepared_batch = list(res_obj.get("batch") or [])
            if not messages.get("user"): return _ng("ValidationError","Empty user prompt")
        else:
            messages = dict(p.get("messages") or {})
            prepared_batch = list(p.get("prepared_batch") or [])
            if not messages.get("user"): return _ng("InvalidPayload","messages required when run_build_prompts=false")

        # PREPARE RUN DIR
        rd = RunDirs(Path(out_base))
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        rd_obj = rd.ensure(run_id)
        run_root = Path(rd_obj.root)
        bundle_root = run_root / "bundle"
        bundle_root.mkdir(parents=True, exist_ok=True)

        # CODE BUNDLE (make → inject), with robust fallbacks
        bundle: Dict[str, Any] = {}
        if include_manifest:
            # try NEW API
            try:
                res = _call_provider(bundle_make_v1, {
                    "mode": code_bundle_mode, "publish_github": publish_github,
                    "chunk_manifest": chunk_manifest, "split_bytes": split_bytes,
                    "group_dirs": group_dirs, "out_base": out_base,
                    "run_dir": str(run_root), "project_root": os.getcwd(),
                    "exclude_globs": exclude_globs,
                }, ctx)
                art, meta = _first_artifact(res)
            except Exception as e:
                art, meta = None, {}

            need_legacy = False
            if not art:
                need_legacy = True
            elif art.kind == "Problem":
                prob = (meta or {}).get("problem", {}) if isinstance(meta, dict) else {}
                msg = str(prob.get("message","")).lower()
                need_legacy = ("root" in msg and "out_file" in msg)

            if need_legacy:
                # legacy: requires 'root' and 'out_file'
                manifest_path = bundle_root / "design_manifest.jsonl"
                try:
                    res2 = _call_provider(bundle_make_v1, {
                        "root": os.getcwd(),
                        "out_file": str(manifest_path),
                        "exclude_globs": exclude_globs,
                    }, ctx)
                    art2, meta2 = _first_artifact(res2)
                except Exception:
                    art2, meta2 = None, {}
                if art2 and art2.kind != "Problem" and manifest_path.exists():
                    bundle = {
                        "root": str(bundle_root),
                        "assistant_handoff": str(bundle_root / "assistant_handoff.v1.json"),
                        "manifest": str(manifest_path),
                        "parts_index": "",
                        "parts_dir": "",
                        "is_chunked": False,
                        "split_bytes": split_bytes,
                        "run_dir": str(run_root),
                        "mode": code_bundle_mode,
                        "group_dirs": group_dirs,
                    }
                else:
                    # FINAL FALLBACK: build manifest locally via utils_code_index
                    res3 = _call_provider(utils_code_index_v1, {"project_root": os.getcwd(), "exclude_globs": exclude_globs}, ctx)
                    a3, m3 = _first_artifact(res3)
                    files = list((_meta_pick(m3, "result", default={}) or {}).get("items") or m3.get("items") or [])
                    manifest_path = bundle_root / "design_manifest.jsonl"
                    size = _write_manifest_monolith(manifest_path, files)
                    is_chunked = False
                    if chunk_manifest in {"always"} or (chunk_manifest == "auto" and size > max(1, split_bytes)):
                        parts_dir = bundle_root / "design_manifest"
                        parts_idx = bundle_root / "design_manifest_parts_index.json"
                        _write_manifest_chunked(parts_dir, parts_idx, files, split_bytes)
                        bundle = {
                            "root": str(bundle_root),
                            "assistant_handoff": str(bundle_root / "assistant_handoff.v1.json"),
                            "manifest": "",
                            "parts_index": str(parts_idx),
                            "parts_dir": str(parts_dir),
                            "is_chunked": True,
                            "split_bytes": split_bytes,
                            "run_dir": str(run_root),
                            "mode": code_bundle_mode,
                            "group_dirs": group_dirs,
                        }
                    else:
                        bundle = {
                            "root": str(bundle_root),
                            "assistant_handoff": str(bundle_root / "assistant_handoff.v1.json"),
                            "manifest": str(manifest_path),
                            "parts_index": "",
                            "parts_dir": "",
                            "is_chunked": False,
                            "split_bytes": split_bytes,
                            "run_dir": str(run_root),
                            "mode": code_bundle_mode,
                            "group_dirs": group_dirs,
                        }
            else:
                bundle = dict(_meta_pick(meta, "result", "bundle", default={}) or {})
                bundle.setdefault("run_dir", str(run_root))
                bundle.setdefault("root", str(bundle_root))

            # inject prompt
            res = _call_provider(bundle_inject_prompt_v1, {
                "bundle": bundle, "messages": messages, "ask_spec": ask_spec,
                "prepared_batch": prepared_batch,
                "bundle_meta": {
                    "mode": code_bundle_mode, "chunk_manifest": chunk_manifest,
                    "split_bytes": split_bytes, "group_dirs": group_dirs,
                },
            }, ctx)
            art, meta = _first_artifact(res)
            if not art: return _ng("ProviderError","packager_bundle_inject_prompt.v1 returned nothing")
            if art.kind == "Problem":
                prob = (meta or {}).get("problem", {})
                return _ng(prob.get("code","ProviderProblem"), prob.get("message","bundle inject failed"), details=prob)
            bundle = dict(_meta_pick(meta, "result", "bundle", default=bundle) or bundle)

        # LLM
        if run_llm:
            batches = []
            for idx, it in enumerate(items):
                batches.append({
                    "messages": [
                        {"role": "system", "content": messages.get("system", "")},
                        {"role": "user", "content": messages.get("user", "")},
                    ],
                    "ask_spec": ask_spec,
                    "id": it.get("id", idx),
                })
            llm_payload = {"provider": provider, "model": model, "batches": batches, "ask_spec": ask_spec, "bundle": bundle}
            res = _call_provider(llm_complete_batches_v1, llm_payload, ctx)
            art, meta = _first_artifact(res)
            if not art: return _ng("ProviderError","llm.complete_batches.v1 returned nothing")
            if art.kind == "Problem":
                prob = (meta or {}).get("problem", {})
                return _ng(prob.get("code","ProviderProblem"), prob.get("message","LLM failed"), details=prob)
            raw_results = _meta_pick(meta, "results", "result", default=[])
            results_list = list(raw_results) if isinstance(raw_results, list) else []
            if not results_list: return _ng("ValidationError","LLM returned no results")
        else:
            results_list = list(p.get("results") or [])

        # UNPACK → SANITIZE → VERIFY
        up_res = _call_provider(results_unpack_v1, {"results": results_list, "results_map": {i:v for i,v in enumerate(results_list)}}, ctx)
        art, meta = _first_artifact(up_res)
        parsed_items = list(_meta_pick(meta, "items", "result", default=[]))
        if run_sanitize:
            san_res = _call_provider(docstrings_sanitize_v1, {"items": parsed_items, "prepared_items": prepared_batch}, ctx)
            art, meta = _first_artifact(san_res)
            sanitized = list(_meta_pick(meta, "items", "result", default=parsed_items))
        else:
            sanitized = parsed_items
        if run_verify:
            ver_res = _call_provider(docstrings_verify_v1, {"items": sanitized}, ctx)
            art, meta = _first_artifact(ver_res)
            verified = list(_meta_pick(meta, "items", "result", default=sanitized))
        else:
            verified = sanitized

        # SAVE/APPLY (same run_dir)
        patched = _call_provider(patch_run_v1, {
            "items": sanitized, "out_base": out_base, "write": True, "dry_run": False,
            "provider": provider, "model": model, "ask_spec": ask_spec,
            "sqlalchemy_url": sqlalchemy_url, "sqlalchemy_table": sqlalchemy_table,
            "llm": {"provider": provider, "model": model, "ask_spec": ask_spec},
            "engine": {"provider": provider, "model": model, "ask_spec": ask_spec},
            "llm_provider": provider, "llm_model": model,
            "raw_prompts": messages, "raw_responses": results_list, "prepared_batch": prepared_batch,
            "verify_summary": {"count": len(verified), "errors": 0},
            "run_dir": str(run_root),
        }, ctx)
        art, meta = _first_artifact(patched)
        result_patched = (meta.get("result") if isinstance(meta, dict) else None) or meta or {}

        return _ok({
            "stats": {
                "fetched": len(items), "built": len(items),
                "completed": len(results_list), "unpacked": len(parsed_items),
                "sanitize_errors": 0, "verify_errors": 0,
                "patches_saved": len(result_patched.get("patches") or []),
                "patches_applied": 0, "archived": 0, "rolled_back": 0,
                "model": model, "provider": provider, "table": sqlalchemy_table,
                "status_filter": status_filter, "out_base": out_base,
                "code_bundle_mode": code_bundle_mode, "chunk_manifest": chunk_manifest,
                "split_bytes": split_bytes, "group_dirs": group_dirs
            },
            "bundle": bundle,
            "patched": result_patched,
        })

    finally:
        _ENGINE_DEPTH -= 1


__all__ = ["run_v1"]







