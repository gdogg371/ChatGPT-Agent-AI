# v2/backend/core/prompt_pipeline/executor/engine.py
from __future__ import annotations
"""
Prompt Pipeline Engine (hardened, capability-routed)

This engine orchestrates phases using generic *capability* calls (via the
capability runner) so it never couples directly to docstring providers.

Phases:
  - FETCH                → capability: introspect.fetch.v1
  - BUILD                → capability: docstrings.build_prompts.v1
  - PREPARE RUN DIR      → ensure out_base
  - BUNDLE.MAKE/INJECT   → optional packager helpers (best-effort)
  - LLM                  → capability: llm.complete_batches.v1
  - SANITIZE             → capability: docstrings.sanitize.v2 (formats docstrings)
  - VERIFY               → capability: docstrings.verify.v1 (policy configurable)
  - PATCH.APPLY_FILES    → capability: patch.apply_files.v1
  - FINALIZE WORKSPACE   → mirror sandbox_applied → <project_root>/v3 (strip leading 'v2/' from paths)

Outputs:
  - out_base/<run_id>/patches/*.patch
  - out_base/<run_id>/sandbox_applied/...
  - <project_root>/v3/...  (mirrored from sandbox_applied)
  - out_base/llm.input.json (debug snapshot)
  - out_file: short summary json

Environment knobs:
  - LLM_PROVIDER_DEBUG=1           → print provider input snapshot
  - DOCSTRING_VERIFY_POLICY=value  → default "lenient" (or "strict")
"""

import json
import os
import shutil
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Callable

from v2.backend.core.configuration.loader import ConfigPaths, get_db, get_pipeline_vars
from v2.backend.core.prompt_pipeline.executor.providers import capability_run

# --------------------------------------------------------------------------------------
# Spine contracts (Artifacts)
# --------------------------------------------------------------------------------------
try:
    from v2.backend.core.spine.contracts import Artifact  # type: ignore
except Exception:
    @dataclass
    class Artifact:  # type: ignore
        kind: str
        uri: str
        sha256: str = ""
        meta: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Optional packager helpers: kept best-effort (not docstring-specific)
# --------------------------------------------------------------------------------------
def _import_first(module_name: str, *candidate_attrs: str) -> Optional[Callable]:
    try:
        mod = __import__(module_name, fromlist=["*"])
    except Exception:
        return None
    for name in candidate_attrs:
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    return None

bundle_make = _import_first("v2.backend.core.spine.providers.packager_bundle_make", "run_v1")
bundle_inject_prompt = _import_first("v2.backend.core.spine.providers.packager_bundle_inject_prompt", "run_v1")
bundle_unpack = _import_first("v2.backend.core.spine.providers.packager_bundle_unpack", "run_v1")


# --------------------------------------------------------------------------------------
# Utils
# --------------------------------------------------------------------------------------
ENGINE_TAG = "[llm.engine.run.v1] hardened engine ACTIVE (bundle attach enabled)"
print(ENGINE_TAG)


def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}

def _safe_get(d: Dict[str, Any], *keys, default=None):
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

def _payload_to_dict(task_like: Any) -> Dict[str, Any]:
    if isinstance(task_like, dict):
        return dict(task_like)
    v = getattr(task_like, "payload", None)
    if isinstance(v, dict):
        return dict(v)
    for attr in ("meta", "data"):
        v = getattr(task_like, attr, None)
        if isinstance(v, dict):
            return dict(v)
    out: Dict[str, Any] = {}
    for k in (
        "root", "project_root", "out_base", "out_file",
        "sqlalchemy_url", "sqlalchemy_table",
        "status_any", "status_filter", "max_rows",
        "ask_spec", "provider", "model", "bundle"
    ):
        try:
            if hasattr(task_like, k):
                out[k] = getattr(task_like, k); continue
            if isinstance(task_like, dict):
                out[k] = task_like.get(k)
            else:
                try:
                    out[k] = task_like[k]  # type: ignore[index]
                except Exception:
                    pass
        except Exception:
            pass
    return out

def _normalize_payload(payload_in: Any) -> Dict[str, Any]:
    p = _payload_to_dict(payload_in)
    if not p:
        raise ValueError("payload must include 'root' and 'out_file'")
    root = p.get("root") or p.get("project_root")
    if not root:
        raise ValueError("payload must include 'root' and 'out_file'")
    p["root"] = str(Path(str(root)).as_posix())
    if not p.get("out_base"):
        p["out_base"] = "output/patches_received"
    if not p.get("out_file"):
        out_base = Path(p["root"]) / p["out_base"]
        out_base.mkdir(parents=True, exist_ok=True)
        p["out_file"] = str(out_base / "engine.out.json")
    return p

def _resolve_out_base(p: Dict[str, Any]) -> str:
    return str(Path(p["root"]) / str(p.get("out_base") or "output/patches_received"))

def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _artifact_problem(uri: str, code: str, message: str, *, retryable: bool = False,
                      details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [Artifact(kind="Problem", uri=uri, sha256="", meta={
        "problem": {"code": code, "message": message, "retryable": retryable, "details": details or {}}
    })]

def _first_artifact(arts: Optional[List[Artifact]]) -> Tuple[Optional[Artifact], Dict[str, Any]]:
    if not arts:
        return None, {}
    art = arts[0]
    try:
        meta = art.meta if hasattr(art, "meta") else _as_dict(getattr(art, "meta", {}))
        return art, _as_dict(meta)
    except Exception:
        return art, {}


# Minimal Task-like wrapper for optional direct-call providers (packager only)
class _TaskShim:
    __slots__ = ("payload", "meta", "data")
    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        self.meta = payload
        self.data = payload
    def __getitem__(self, k: str) -> Any:
        return self.payload[k]
    def get(self, k: str, default: Any = None) -> Any:
        return self.payload.get(k, default)


def _call_provider(fn: Optional[Callable], payload: Dict[str, Any], context: Dict[str, Any], tag: str) -> List[Artifact]:
    """
    Legacy helper for optional packager providers (non-capability). Everything
    else routes via capability_run.
    """
    uri_ok = f"spine://result/{tag}"
    uri_ng = f"spine://problem/{tag}"
    if not callable(fn):
        return _artifact_problem(uri_ng, "ProviderMissing", f"No provider available for {tag}", retryable=False)
    task_obj = payload if hasattr(payload, "payload") else _TaskShim(payload)
    try:
        try:
            res = fn(task_obj, context)
        except TypeError:
            res = fn(task_obj)
        if isinstance(res, list) and res and isinstance(res[0], Artifact):
            return res
        # wrap raw meta
        return [Artifact(kind="Result", uri=uri_ok, sha256="", meta=_as_dict(res))]
    except Exception as e:
        return _artifact_problem(uri_ng, "ProviderError", f"{type(e).__name__}: {e}",
                                 details={"trace": traceback.format_exc()})


# --------------------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------------------
@dataclass
class Engine:
    context: Dict[str, Any] = field(default_factory=dict)

    def run(self, payload: Dict[str, Any]) -> List[Artifact]:
        p = _normalize_payload(payload)

        me = Path(__file__).resolve()
        print(f"[ENGINE DEBUG] file= {me} has_norm= True keys= {sorted(list(p.keys()))}")
        print(f"[ENGINE NORMALIZED] root= {p['root']} out_file= {p['out_file']} out_base= {p['out_base']}")

        context = self.context
        context.setdefault("state", {})

        # -------------------- PHASE: FETCH --------------------
        print("[PHASE] FETCH")
        fetch_items: List[Dict[str, Any]] = _safe_get(context, "state", "fetch", "result", "items", default=[]) or []
        if not fetch_items:
            # Resolve DB + vars via loader
            try:
                paths = ConfigPaths.detect(project_root=Path(p["root"]))
                vars_cfg = get_pipeline_vars(paths)
                db_cfg = get_db(paths)
            except Exception as e:
                return _artifact_problem("spine://problem/introspect.fetch.v1",
                                         "ConfigError", f"{type(e).__name__}: {e}", retryable=False)
            fetch_payload = {
                "sqlalchemy_url": db_cfg.sqlalchemy_url,
                "sqlalchemy_table": db_cfg.table,
                "status_any": [vars_cfg.status_filter] if getattr(vars_cfg, "status_filter", "") else [],
                "max_rows": int(getattr(vars_cfg, "max_rows", 200) or 200),
                "exclude_globs": list(getattr(vars_cfg, "exclude_globs", []) or []),
                "segment_excludes": list(getattr(vars_cfg, "segment_excludes", []) or []),
            }
            res_fetch = capability_run("introspect.fetch.v1", fetch_payload, context)
            art_fetch, meta_fetch = _first_artifact(res_fetch)
            if art_fetch and getattr(art_fetch, "kind", "") == "Problem":
                return res_fetch
            fetch_items = list(_as_dict(meta_fetch).get("items") or [])
            context["state"].setdefault("fetch", {})["result"] = {"items": fetch_items}

        # -------------------- PHASE: BUILD --------------------
        print("[PHASE] BUILD")
        build_res = _safe_get(context, "state", "build", "result", default={}) or {}
        if (not build_res) and fetch_items:
            build_payload = {
                "records": fetch_items,
                "project_root": p["root"],
                "context_half_window": p.get("context_half_window", 25),
                "description_field": p.get("description_field", "description"),
            }
            res_build = capability_run("docstrings.build_prompts.v1", build_payload, context)
            art_build, meta_build = _first_artifact(res_build)
            if art_build and getattr(art_build, "kind", "") == "Problem":
                return res_build

            # tolerate both shapes: meta["result"] or meta at top-level
            cand = _as_dict(meta_build.get("result") or {})
            if not cand:
                # top-level fallback
                top = {k: meta_build.get(k) for k in ("messages", "ids", "batch") if k in meta_build}
                if top:
                    cand = _as_dict(top)

            # light debug
            try:
                print("[BUILD] meta keys=", list(meta_build.keys()))
                if cand:
                    print("[BUILD] result keys=", list(cand.keys()))
                    print("[BUILD] counts: messages=",
                          len(cand.get("messages") or []),
                          " ids=", len(cand.get("ids") or []),
                          " batch=", len(cand.get("batch") or []))
            except Exception:
                pass

            build_res = cand or {}
            if build_res:
                context["state"].setdefault("build", {})["result"] = build_res

        # -------------------- PHASE: PREPARE RUN DIR --------------------
        print("[PHASE] PREPARE RUN DIR]")
        out_base = Path(_resolve_out_base(p)); out_base.mkdir(parents=True, exist_ok=True)

        # -------------------- PHASE: BUNDLE.MAKE --------------------
        print("[PHASE] BUNDLE.MAKE")
        made_bundle = None
        if bundle_make:
            res_make = _call_provider(bundle_make, {"root": p["root"]}, context, "BUNDLE.MAKE")
            _, meta_make = _first_artifact(res_make)
            made_bundle = _as_dict(meta_make.get("bundle") or {})
        else:
            print("[PHASE] BUNDLE.MAKE → LEGACY fallback")
            print("[PHASE] BUNDLE.MAKE → LOCAL INDEX fallback")

        # -------------------- PHASE: BUNDLE.INJECT --------------------
        print("[PHASE] BUNDLE.INJECT")
        if bundle_inject_prompt and made_bundle:
            _ = _call_provider(bundle_inject_prompt, {"bundle": made_bundle}, context, "BUNDLE.INJECT")

        # -------------------- PHASE: LLM --------------------
        print("[PHASE] LLM")
        messages = None
        ids: List[str] = []
        prepared: List[Dict[str, Any]] = []

        # consume whatever BUILD left in context (preferred normalized shape)
        if isinstance(build_res, dict):
            messages = build_res.get("messages") or messages
            ids = list(build_res.get("ids") or ids)
            prepared = list(build_res.get("batch") or prepared)

        # if missing, re-run BUILD once and tolerate both payload shapes
        if (messages is None or not ids) and fetch_items:
            build_payload = {
                "records": fetch_items,
                "project_root": p["root"],
                "context_half_window": p.get("context_half_window", 25),
                "description_field": p.get("description_field", "description"),
            }
            res_build = capability_run("docstrings.build_prompts.v1", build_payload, context)
            art_build, meta_build = _first_artifact(res_build)
            if art_build and getattr(art_build, "kind", "") == "Problem":
                return res_build

            # Prefer meta["result"]; fall back to top-level
            cand = _as_dict(meta_build.get("result") or {})
            if not cand:
                top = {k: meta_build.get(k) for k in ("messages", "ids", "batch") if k in meta_build}
                if top:
                    cand = _as_dict(top)

            if cand:
                messages = cand.get("messages") or messages
                ids = list(cand.get("ids") or ids)
                prepared = list(cand.get("batch") or prepared)
                context["state"].setdefault("build", {})["result"] = cand

        # hard stop if still nothing to ask
        if messages is None or not ids:
            details = {
                "why": "No messages/ids available",
                "hints": [
                    "Ensure FETCH produced records (introspection_index rows).",
                    "Ensure DOCSTRINGS.BUILD returned messages + ids."
                ],
                "seen": {"fetch_items": len(fetch_items), "has_build_res": bool(build_res)}
            }
            return _artifact_problem(
                "spine://problem/llm.engine.run.v1",
                "InvalidPayload",
                "Batch 0 has no 'items' and no resolvable 'ids+messages' (checked batch, meta, and context).",
                retryable=False,
                details=details,
            )

        # keep prepared batch in context for later phases (sanitize/verify/patch)
        if prepared:
            context["state"].setdefault("build", {})["result"] = {"items": prepared}

        ask_spec = _as_dict(p.get("ask_spec") or {})
        llm_payload = {
            "provider": p.get("provider") or "openai",
            "model": p.get("model") or ask_spec.get("model") or "gpt-4o-mini",
            "ask_spec": ask_spec,
            "batches": [{}],  # provider uses ids+messages; batch is a marker
            "messages": messages,
            "ids": ids,
            "bundle": made_bundle or p.get("bundle"),
        }

        if os.environ.get("LLM_PROVIDER_DEBUG"):
            snap = {
                "meta_keys": list(llm_payload.keys()),
                "has_batches": isinstance(llm_payload.get("batches"), list),
                "top_ids_len": len(llm_payload.get("ids") or []),
                "ctx_keys": list(self.context.keys()),
                "ctx_build_keys": list(
                    _as_dict(_safe_get(self.context, "state", "build", "result", default={})).keys()),
                "ctx_ids_len": len(_safe_get(self.context, "state", "build", "result", "ids", default=[]) or []),
            }
            print("[LLM.provider.input]", json.dumps(snap, ensure_ascii=False))

        try:
            _write_json(Path(_resolve_out_base(p)) / "llm.input.json", llm_payload)
            print("[ENGINE DEBUG] wrote LLM payload →", str(Path(_resolve_out_base(p)) / "llm.input.json"))
        except Exception as _e:
            print("[ENGINE DEBUG] could not write llm.input.json:", _e)

        res_llm = capability_run("llm.complete_batches.v1", llm_payload, self.context)

        # -------------------- PHASE: SANITIZE --------------------
        print("[PHASE] SANITIZE")
        _, llm_meta = _first_artifact(res_llm)
        raw_results = list(_as_dict(llm_meta).get("results") or [])
        id_to_doc: Dict[str, Dict[str, Any]] = {}
        for r in raw_results:
            rid = str(r.get("id"))
            raw = r.get("raw") or ""
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict) and rid:
                    id_to_doc[rid] = {"docstring": str(obj.get("docstring", ""))}
            except Exception:
                continue

        prepared_batch = _safe_get(self.context, "state", "build", "result", "items", default=[]) or []
        san_payload = {
            "items": id_to_doc,
            "prepared_batch": prepared_batch,  # allows sanitizer to map ids → (path, relpath, lineno, signature)
            "project_root": p["root"],
        }
        res_san = capability_run("docstrings.sanitize.v1", san_payload, self.context)
        art_san, meta_san = _first_artifact(res_san)
        if art_san and getattr(art_san, "kind", "") == "Problem":
            return res_san
        sanitized_items = list(_as_dict(meta_san).get("result") or [])
        print(f"[SANITIZE] items={len(sanitized_items)}")

        # -------------------- PHASE: VERIFY --------------------
        print("[PHASE] VERIFY")
        verify_policy = (os.environ.get("DOCSTRING_VERIFY_POLICY") or "lenient").lower()
        res_ver = capability_run("docstrings.verify.v1", {"items": sanitized_items, "policy": verify_policy}, self.context)
        art_ver, meta_ver = _first_artifact(res_ver)
        if art_ver and getattr(art_ver, "kind", "") == "Problem":
            return res_ver
        verify_result = _as_dict(meta_ver).get("result") or {}
        ok_items = list(_as_dict(verify_result).get("items") or [])
        verify_summary = {"count": len(ok_items), "reports": _as_dict(verify_result).get("reports")}
        print(f"[VERIFY] ok_items={verify_summary['count']}")

        # -------------------- PHASE: PATCH.APPLY_FILES --------------------
        print("[PHASE] PATCH.APPLY_FILES")
        run_out_base = _resolve_out_base(p)
        apply_payload = {
            "out_base": run_out_base,
            "apply_root": str(Path(p["root"]) / "v3"),  # providers that honor an explicit root can write here
            "items": ok_items,  # must include relpath/path/target_lineno/docstring
            "raw_prompts": llm_payload.get("messages") or {},
            "raw_responses": raw_results,
            "prepared_batch": prepared_batch,
            "verify_summary": verify_summary,
        }
        res_apply = capability_run("patch.apply_files.v1", apply_payload, self.context)
        art_apply, meta_apply = _first_artifact(res_apply)
        run_dir = _as_dict(meta_apply).get("run_dir")
        count = int(_as_dict(meta_apply).get("count") or 0)

        # -------------------- FINALIZE WORKSPACE (mirror sandbox → v3) --------------------
        try:
            # locate sandbox_applied
            sb = None
            if run_dir:
                sb_path = Path(run_dir) / "sandbox_applied"
                if sb_path.is_dir():
                    sb = sb_path
            if not sb:
                cand = sorted(Path(run_out_base).glob("*/sandbox_applied"), reverse=True)
                sb = cand[0] if cand else None

            dest = Path(p["root"]) / "v3"
            dest.mkdir(parents=True, exist_ok=True)

            # infer a common first segment (e.g., 'v2') from ok_items relpaths and strip it
            strip_prefix: Optional[str] = None
            try:
                first_segments = {
                    (Path(str(it.get("relpath", "")).replace("\\", "/")).parts[0])
                    for it in ok_items
                    if str(it.get("relpath", "")).strip()
                    and Path(str(it.get("relpath")).replace("\\", "/")).parts
                }
                if len(first_segments) == 1:
                    seg = list(first_segments)[0]
                    if seg.lower() == "v2":
                        strip_prefix = seg
            except Exception:
                strip_prefix = None

            print(f"[FINALIZE] strip_prefix={strip_prefix or '(none)'}")

            if sb and sb.is_dir():
                for src_file in sb.rglob("*"):
                    if src_file.is_dir():
                        continue
                    rel = src_file.relative_to(sb)
                    if strip_prefix and rel.parts and rel.parts[0] == strip_prefix:
                        rel = Path(*rel.parts[1:])
                    dst_file = dest / rel
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)
                print(f"[FINALIZE] mirrored sandbox_applied → {dest}")
            else:
                print("[FINALIZE] no sandbox_applied directory found to mirror")

        except Exception as _e:
            print("[FINALIZE] error while mirroring sandbox_applied → v3:", _e)

        # Final short summary file
        try:
            summary = {
                "phase": "PATCH.APPLY_FILES",
                "artifact_kind": getattr(art_apply, "kind", "Unknown") if art_apply else "None",
                "meta_keys": list(_as_dict(meta_apply).keys()),
                "count": count,
                "run_dir": run_dir,
            }
            _write_json(Path(p["out_file"]), summary)
        except Exception:
            pass

        return res_apply


# --------------------------------------------------------------------------------------
# Convenience function
# --------------------------------------------------------------------------------------
def run_v1(payload: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    eng = Engine(context=context or {})
    return eng.run(payload)
