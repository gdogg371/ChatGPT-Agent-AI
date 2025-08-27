# v2/backend/core/prompt_pipeline/executor/engine.py
r"""
LLM Engine (spine capability: llm.engine.run.v1)

Hardened features:
- Clear sentinel print to confirm this file is executing.
- Re-entrancy guard to prevent patch.run <-> engine recursion.
- Robust provider calling via a light _TaskShim wrapper (mapping-compatible).
- Results normalization:
    * results -> list[dict]
    * results_map -> dict with BOTH int and str keys (0 and "0" point to same item)
- Stage guards around fetch/build/LLM/unpack to capture KeyError and print traces.
- Global wrapper to catch *any* unhandled exception and return a Problem with traceback.
- provider/model pulled from llm.yml and sqlalchemy_url/table pulled from db.yml when absent.
- BUILD → SANITIZE baton: carry prepared items through context and payload.
- SAVE/APPLY: forwards provider/model/ask_spec and DB fields; accepts varied patcher meta shapes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import os

from v2.backend.core.spine.contracts import Artifact

# Providers used by the engine
from v2.backend.core.introspect.providers import fetch_v1 as introspect_fetch_v1
from v2.backend.core.prompt_pipeline.executor.providers import (
    unpack_results_v1 as results_unpack_v1,
)
from v2.backend.core.docstrings.providers import (
    build_prompts_v1 as doc_prompts_build_v1,
)

from v2.backend.core.prompt_pipeline.llm.providers import (
    complete_batches_v1 as llm_complete_batches_v1,
)
from v2.backend.core.prompt_pipeline.llm.response_parser import parse_json_response
from v2.backend.core.docstrings.providers import (
    sanitize_outputs_v1 as docstrings_sanitize_v1,
    verify_batch_v1 as docstrings_verify_v1,
)
from v2.backend.core.patch_engine.providers import run_v1 as patch_run_v1


# ---------------- global safety wrapper: ensure Problems instead of hard crashes ---------------

def _wrap_exceptions(fn):
    def inner(task, context):
        try:
            return fn(task, context)
        except KeyError as e:
            import traceback as _tb
            tb = _tb.format_exc()
            print("[llm.engine.run.v1] Unhandled KeyError; trace follows:\n" + tb)
            return [
                Artifact(
                    kind="Problem",
                    uri="spine://problem/llm.engine.run.v1",
                    sha256="",
                    meta={
                        "problem": {
                            "code": "UnhandledKeyError",
                            "message": f"KeyError: {e}",
                            "retryable": False,
                            "details": {"trace": tb},
                        }
                    },
                )
            ]
        except Exception as e:
            import traceback as _tb
            tb = _tb.format_exc()
            print("[llm.engine.run.v1] Unhandled Exception; trace follows:\n" + tb)
            return [
                Artifact(
                    kind="Problem",
                    uri="spine://problem/llm.engine.run.v1",
                    sha256="",
                    meta={
                        "problem": {
                            "code": "UnhandledException",
                            "message": f"{type(e).__name__}: {e}",
                            "retryable": False,
                            "details": {"trace": tb},
                        }
                    },
                )
            ]
    return inner


# -------------------------- tiny shim & helpers --------------------------

class _TaskShim:
    """Task-like wrapper that behaves as a mapping of the *payload*.
    - Attribute access (task.payload) still works for well-behaved providers.
    - Mapping conversion (dict(task)) returns the *flat payload* so downstream
      code that incorrectly does dict(task) still receives the expected top-level
      fields (provider, model, sqlalchemy_url, ...).
    """
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload: Dict[str, Any] = payload
        self.envelope: Dict[str, Any] = {}
        self.payload_schema: Dict[str, Any] = {}

    # Mapping protocol -> expose the *payload* keys/values
    def keys(self):
        return self.payload.keys()

    def __iter__(self):
        return iter(self.payload)

    def __len__(self):
        return len(self.payload)

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    # Convenience (some providers call .get/.items on the task)
    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    def items(self):
        return self.payload.items()


def _ok(meta: Dict[str, Any]) -> List[Artifact]:
    return [
        Artifact(
            kind="Result",
            uri="spine://result/llm.engine.run.v1",
            sha256="",
            meta=meta,
        )
    ]


def _ng(code: str, message: str, *, retryable: bool = False, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [
        Artifact(
            kind="Problem",
            uri="spine://problem/llm.engine.run.v1",
            sha256="",
            meta={
                "problem": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "details": details or {},
                }
            },
        )
    ]


def _as_bool(x: Any, default: bool = False) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        s = x.strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
    return default


def _ensure_items(obj: Any) -> List[Dict[str, Any]]:
    if isinstance(obj, list):
        return [dict(it) for it in obj if isinstance(it, dict)]
    return []


def _call_provider(fn, payload: Dict[str, Any], context: Dict[str, Any]) -> Any:
    """Call a provider with a shim Task to avoid strict Task typing issues."""
    t = _TaskShim(payload)
    try:
        return fn(t, context or {})
    except TypeError:
        # Some providers accept (task) only
        return fn(t)


def _first_artifact(res: Any) -> (Optional[Artifact], Dict[str, Any]):
    """Return (first_artifact, its_meta_dict). Tolerates dict-only returns."""
    if isinstance(res, list) and res:
        art = res[0]
        if isinstance(art, Artifact):
            return art, dict(art.meta or {})
        if isinstance(art, dict):
            a = Artifact(kind="Result", uri="spine://shim/result", sha256="", meta=art)
            return a, dict(art)
        return None, {}
    if isinstance(res, dict):
        a = Artifact(kind="Result", uri="spine://shim/result", sha256="", meta=res)
        return a, dict(res)
    return None, {}


def _meta_pick(meta: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in meta and meta[k] is not None:
            return meta[k]
    return default


# Re-entrancy depth guard — must be module-global
_ENGINE_DEPTH = 0


# ------------------------------- main entrypoint -------------------------------

def run_v1(task: _TaskShim, context: Dict[str, Any]) -> List[Artifact]:
    """
    Spine capability: llm.engine.run.v1
    Orchestrates fetch → prompt build → LLM → unpack → sanitize → verify → save/apply.
    Hardened for nested invocations, config fallbacks, and provider quirks.
    """
    print("[llm.engine.run.v1] hardened engine ACTIVE (int+str results_map keys)")

    p = dict(task.payload or {})

    # mutable pipeline context we pass along (so providers can see build state)
    ctx = dict(context or {})
    prepared_for_context: List[Dict[str, Any]] = []

    # Re-entrancy guard: prevent patch.run.v1 ↔ engine recursion from re-saving/applying
    global _ENGINE_DEPTH
    _ENGINE_DEPTH += 1
    try:
        # -------- Required (use llm.yml + db.yml defaults if not provided) ----------
        try:
            from v2.backend.core.configuration.loader import get_llm  # lazy import
            _llm_cfg = get_llm()  # reads config/spine/pipelines/<profile>/llm.yml
            _default_provider = str(getattr(_llm_cfg, "provider", "") or getattr(_llm_cfg, "name", "") or "").strip()
            _default_model = str(getattr(_llm_cfg, "model", "") or "").strip()
        except Exception:
            _default_provider = ""
            _default_model = ""

        try:
            from v2.backend.core.configuration.loader import get_db  # lazy import
            _db_cfg = get_db()  # reads config/spine/pipelines/<profile>/db.yml
            _default_sqlalchemy_url = str(getattr(_db_cfg, "sqlalchemy_url", "") or "").strip()
            _default_sqlalchemy_table = str(
                getattr(_db_cfg, "sqlalchemy_table", "") or getattr(_db_cfg, "table", "") or ""
            ).strip()
        except Exception:
            _default_sqlalchemy_url = ""
            _default_sqlalchemy_table = ""

        provider = str(p.get("provider") or _default_provider).strip()
        model = str(p.get("model") or _default_model).strip()
        sqlalchemy_url = str(p.get("sqlalchemy_url") or _default_sqlalchemy_url).strip()
        sqlalchemy_table = str(p.get("sqlalchemy_table") or _default_sqlalchemy_table or "introspection_index").strip()

        if not sqlalchemy_url or not sqlalchemy_table:
            return _ng("InvalidPayload", "Missing sqlalchemy_url/sqlalchemy_table (db.yml defaults not found)")
        if not provider or not model:
            return _ng("InvalidPayload", "Missing provider/model (llm.yml defaults not found)")

        # -------- Stage knobs / options ----------
        status_filter = str(p.get("status_filter") or "").strip()
        max_rows = int(p.get("max_rows") or 200)
        exclude_globs = list(p.get("exclude_globs") or [])
        segment_excludes = list(p.get("segment_excludes") or [])
        out_base = str(p.get("out_base") or "").strip()
        ask_spec = dict(p.get("ask_spec") or {})

        # Canonical toggles
        run_fetch = _as_bool(p.get("run_fetch_targets"), True)
        run_build = _as_bool(p.get("run_build_prompts"), True)
        run_llm = _as_bool(p.get("run_run_llm"), True)
        run_unpack = _as_bool(p.get("run_unpack"), True)
        run_sanitize = _as_bool(p.get("run_sanitize"), True)
        run_verify = _as_bool(p.get("run_verify"), True)
        run_save_patch = _as_bool(p.get("run_save_patch"), True)
        run_apply = _as_bool(p.get("run_apply_patch_sandbox"), False)
        run_archive = _as_bool(p.get("run_archive_and_replace"), False)
        run_rollback = _as_bool(p.get("run_rollback"), False)

        # Re-entrancy guard: disable write stages for nested engine calls
        if _ENGINE_DEPTH > 1:
            print("[llm.engine.run.v1] nested call detected; SAVE/APPLY disabled")
            run_save_patch = False
            run_apply = False
            run_archive = False
            run_rollback = False

        # -------- Stats scaffold ----------
        stats: Dict[str, Any] = {
            "fetched": 0,
            "built": 0,
            "completed": 0,
            "unpacked": 0,
            "sanitize_errors": 0,
            "verify_errors": 0,
            "patches_saved": 0,
            "patches_applied": 0,
            "archived": 0,
            "rolled_back": 0,
            "model": model,
            "provider": provider,
            "table": sqlalchemy_table,
            "status_filter": status_filter,
            "out_base": out_base,
        }

        # ------------------------------- FETCH --------------------------------
        items: List[Dict[str, Any]] = []
        if run_fetch:
            fetch_payload = {
                "sqlalchemy_url": sqlalchemy_url,
                "sqlalchemy_table": sqlalchemy_table,
                "status": status_filter,
                "status_filter": status_filter,
                "max_rows": max_rows,
                "exclude_globs": exclude_globs,
                "segment_excludes": segment_excludes,
            }
            try:
                res = _call_provider(introspect_fetch_v1, fetch_payload, ctx)
            except KeyError:
                import traceback as _tb
                print("[llm.engine.run.v1] introspect.fetch.v1 raised KeyError; trace follows:\n" + _tb.format_exc())
                return _ng("ProviderError", "introspect.fetch.v1 raised KeyError (likely [0] on dict)",
                           details={"stage": "introspect.fetch.v1"})
            art, meta = _first_artifact(res)
            if not art:
                return _ng("ProviderError", "introspect.fetch.v1 returned nothing")
            if art.kind == "Problem":
                prob = (meta or {}).get("problem", {})
                return _ng(prob.get("code", "ProviderProblem"), prob.get("message", "fetch failed"), details=prob)

            items = _ensure_items(_meta_pick(meta, "items", "result", default=[]))
            if not items:
                return _ng("ValidationError", "No valid targets found for docstring patching.")
            stats["fetched"] = len(items)
        else:
            items = _ensure_items(p.get("items"))
            if not items:
                return _ng("InvalidPayload", "items required when run_fetch_targets=false")
            stats["fetched"] = len(items)

        # ------------------------------- BUILD PROMPTS ------------------------
        messages: Dict[str, str] = {}
        if run_build:
            # Use the docstrings builder so we get a prepared batch with ids/paths/signatures
            bp = {
                "records": items,  # <- your fetch rows (id, filepath, lineno, symbol_type, ...)
                "project_root": os.getcwd(),  # <- repo root to resolve files for context/signature
                "context_half_window": int(p.get("context_half_window", 25)),
                "description_field": str(p.get("description_field", "description")),
            }
            res = _call_provider(doc_prompts_build_v1, bp, ctx)

            art, meta = _first_artifact(res)
            if not art:
                return _ng("ProviderError", "docstrings.prompts.build.v1 returned nothing")
            if art.kind == "Problem":
                prob = (meta or {}).get("problem", {})
                return _ng(prob.get("code", "ProviderProblem"), prob.get("message", "build prompts failed"),
                           details=prob)

            # messages live in meta.result.messages
            res_obj = _meta_pick(meta, "result", default={}) or {}
            messages = dict(res_obj.get("messages") or {})
            if not messages.get("user"):
                return _ng("ValidationError", "Prompt build returned empty messages.")
            stats["built"] = len(items)

            # capture the prepared batch for downstream lookup (meta.result.batch)
            prepared_for_context = list(res_obj.get("batch") or [])
            # expose it via context so sanitize can find it if explicit payload is absent
            state = ctx.setdefault("state", {})
            state_build = state.setdefault("build", {})
            state_build["result"] = {"items": prepared_for_context}


        else:
            messages = dict(p.get("messages") or {})
            if not messages.get("user"):
                return _ng("InvalidPayload", "messages required when run_build_prompts=false")

        # ------------------------------- LLM (BATCH) --------------------------
        llm_results_list: List[Dict[str, Any]] = []
        llm_results_map: Dict[Any, Dict[str, Any]] = {}

        if run_llm:
            batch = []
            for idx, it in enumerate(items):
                batch.append(
                    {
                        "messages": [
                            {"role": "system", "content": messages.get("system", "")},
                            {"role": "user", "content": messages.get("user", "")},
                        ],
                        "ask_spec": ask_spec,
                        "id": it.get("id", idx),
                    }
                )

            llm_payload = {
                "provider": provider,
                "model": model,
                "batches": batch,
                "ask_spec": ask_spec,
            }

            try:
                res = _call_provider(llm_complete_batches_v1, llm_payload, ctx)
            except KeyError:
                import traceback as _tb
                tb = _tb.format_exc()
                return _ng(
                    "ProviderError",
                    "LLM provider raised KeyError (likely indexing [0] on a dict).",
                    details={"stage": "llm.complete_batches.v1", "trace": tb},
                )

            art, meta = _first_artifact(res)
            if not art:
                return _ng("ProviderError", "llm.complete_batches.v1 returned nothing")
            if art.kind == "Problem":
                prob = (meta or {}).get("problem", {})
                return _ng(prob.get("code", "ProviderProblem"), prob.get("message", "LLM failed"), details=prob)

            raw_results = _meta_pick(meta, "results", "result", default=[])

            # Normalize to BOTH list and dict, with BOTH int and str keys
            if isinstance(raw_results, dict):
                try:
                    keys = sorted(
                        raw_results.keys(),
                        key=lambda k: int(str(k)) if str(k).isdigit() else str(k),
                    )
                except Exception:
                    keys = list(raw_results.keys())

                llm_results_list = []
                llm_results_map = {}
                for i, k in enumerate(keys):
                    v = raw_results[k]
                    if isinstance(v, dict):
                        v = dict(v)
                        llm_results_list.append(v)
                        llm_results_map[i] = v
                        llm_results_map[str(i)] = v
                        llm_results_map[str(k)] = v
                        try:
                            llm_results_map[int(str(k))] = v
                        except Exception:
                            pass

            elif isinstance(raw_results, list):
                llm_results_list = []
                llm_results_map = {}
                for i, v in enumerate(raw_results):
                    if isinstance(v, dict):
                        v = dict(v)
                        llm_results_list.append(v)
                        llm_results_map[i] = v
                        llm_results_map[str(i)] = v
            else:
                llm_results_list, llm_results_map = [], {}

            stats["completed"] = len(llm_results_list)

            if not llm_results_list and not llm_results_map:
                return _ng("ValidationError", "LLM returned no results.")
        else:
            llm_results_list = list(p.get("results") or [])
            llm_results_map = {}
            for i, v in enumerate(llm_results_list):
                llm_results_map[i] = v
                llm_results_map[str(i)] = v
            stats["completed"] = len(llm_results_list)

        # ------------------------------- UNPACK --------------------------------
        parsed_items: List[Dict[str, Any]] = []
        unpack_errors: List[Dict[str, Any]] = []

        if run_unpack:
            up = {
                "results": llm_results_list,
                "results_map": llm_results_map,  # has BOTH int and str keys if built by this engine
            }
            try:
                res = _call_provider(results_unpack_v1, up, ctx)
            except KeyError as e:
                import traceback as _tb
                unpack_errors.append({
                    "stage": "results.unpack.v1",
                    "error": f"KeyError: {e}",
                    "trace": _tb.format_exc(),
                    "hint": "provider likely indexed [0] on a dict",
                })
                res = None
            except Exception as e:
                import traceback as _tb
                unpack_errors.append({
                    "stage": "results.unpack.v1",
                    "error": f"{type(e).__name__}: {e}",
                    "trace": _tb.format_exc(),
                })
                res = None

            art, meta = _first_artifact(res) if res is not None else (None, {})
            if art and art.kind == "Result":
                parsed_items = list(_meta_pick(meta, "items", "result", default=[]))
                unpack_errors.extend(list(meta.get("errors") or []))
            else:
                # Fallback: local parse (list-only path; cannot KeyError on dict keys)
                for i, r in enumerate(llm_results_list):
                    raw = (r.get("raw") if isinstance(r, dict) else None) or ""
                    try:
                        data = parse_json_response(raw)
                        parsed_items.append({"index": i, "data": data})
                    except Exception as e:
                        import traceback as _tb
                        unpack_errors.append({"index": i, "error": f"{type(e).__name__}: {e}", "trace": _tb.format_exc()})

            stats["unpacked"] = len(parsed_items)
        else:
            parsed_items = list(p.get("parsed") or [])
            stats["unpacked"] = len(parsed_items)

        # If parsed items are missing 'id', fill from prepared list (order-aligned)
        if prepared_for_context and parsed_items:
            n = min(len(prepared_for_context), len(parsed_items))
            for i in range(n):
                it = parsed_items[i]
                if isinstance(it, dict):
                    rid = it.get("id")
                    if rid is None or str(rid).strip().lower() == "none" or str(rid).strip() == "":
                        it["id"] = str(prepared_for_context[i].get("id", i))

        # ------------------------------- SANITIZE --------------------------------
        sanitized: List[Dict[str, Any]] = []
        if run_sanitize:
            san_payload = {"items": parsed_items, "prepared_items": prepared_for_context}
            try:
                res = _call_provider(docstrings_sanitize_v1, san_payload, ctx)
            except KeyError:
                import traceback as _tb
                print("[llm.engine.run.v1] sanitize_outputs_v1 raised KeyError; trace follows:\n" + _tb.format_exc())
                sanitized = parsed_items[:]
                stats["sanitize_errors"] = len(parsed_items)
            else:
                art, meta = _first_artifact(res)
                if not art or art.kind == "Problem":
                    sanitized = parsed_items[:]  # non-fatal; continue
                    stats["sanitize_errors"] = len(parsed_items)
                else:
                    sanitized = list(_meta_pick(meta, "items", "result", default=[])) or parsed_items[:]
                    stats["sanitize_errors"] = int(meta.get("errors") or 0)
        else:
            sanitized = parsed_items[:]

        # ------------------------------- VERIFY -----------------------------------
        verified: List[Dict[str, Any]] = []
        if run_verify:
            ver = {"items": sanitized}
            try:
                res = _call_provider(docstrings_verify_v1, ver, ctx)
            except KeyError:
                import traceback as _tb
                print("[llm.engine.run.v1] verify_batch_v1 raised KeyError; trace follows:\n" + _tb.format_exc())
                verified = sanitized[:]
                stats["verify_errors"] = len(sanitized)
            else:
                art, meta = _first_artifact(res)
                if not art or art.kind == "Problem":
                    verified = sanitized[:]
                    stats["verify_errors"] = len(sanitized)
                else:
                    # robust extraction: accept list or dict-with-items
                    v = _meta_pick(meta, "items", default=None)
                    if v is None:
                        r = _meta_pick(meta, "result", default=None)
                        if isinstance(r, list):
                            v = r
                        elif isinstance(r, dict) and "items" in r:
                            v = r["items"]
                    verified = list(v or []) or sanitized[:]
                    stats["verify_errors"] = int(meta.get("errors") or 0)
        else:
            verified = sanitized[:]

        # --- DEBUG: show what we are about to save ---
        try:
            import json as _json
            print(f"[debug] verified items count = {len(verified)}")
            for _i, _it in enumerate(verified[:3]):
                print("[debug] verified[{}] = {}".format(_i, _json.dumps(_it, ensure_ascii=False)[:800]))
        except Exception as _e:
            print("[debug] could not pretty-print verified items:", _e)

        # ------------------------------- SAVE/APPLY/ARCHIVE -----------------------
        patched: Dict[str, Any] = {}
        if run_save_patch:
            # Ensure every verified item carries llm config (in case patcher forwards per-item)
            for it in verified:
                if isinstance(it, dict):
                    it.setdefault("provider", provider)
                    it.setdefault("model", model)
                    it.setdefault("ask_spec", ask_spec)
                    # legacy aliases
                    it.setdefault("llm_provider", provider)
                    it.setdefault("llm_model", model)

            # Send LLM + DB config at top-level and common nested slots some patchers expect
            pr = {
                "items": verified,
                "out_base": out_base,
                "write": True,
                "dry_run": False,
                # top-level
                "provider": provider,
                "model": model,
                "ask_spec": ask_spec,
                "sqlalchemy_url": sqlalchemy_url,
                "sqlalchemy_table": sqlalchemy_table,
                # nested
                "llm": {"provider": provider, "model": model, "ask_spec": ask_spec},
                "engine": {"provider": provider, "model": model, "ask_spec": ask_spec},
                # legacy fallbacks
                "llm_provider": provider,
                "llm_model": model,
            }

            try:
                res = _call_provider(patch_run_v1, pr, ctx)
            except KeyError:
                import traceback as _tb
                print("[llm.engine.run.v1] patch_run_v1 raised KeyError; trace follows:\n" + _tb.format_exc())
                res = None

            art, meta = _first_artifact(res) if res is not None else (None, {})
            patched_meta = (meta.get("result") if isinstance(meta, dict) else None) or (meta or {})
            patched = patched_meta

            def _as_int(x):
                try:
                    return int(x)
                except Exception:
                    return 0

            stats["patches_saved"] = max(
                _as_int(patched_meta.get("count")),
                _as_int(patched_meta.get("saved")),
                _as_int(patched_meta.get("num_patches")),
                len(patched_meta.get("patches") or []),
                0,
            )

        if run_apply:
            stats["patches_applied"] = stats.get("patches_saved", 0)
        if run_archive:
            stats["archived"] = stats.get("patches_saved", 0)
        if run_rollback:
            stats["rolled_back"] = 0  # placeholder

        return _ok({"stats": stats, "unpack_errors": unpack_errors, "patched": patched})

    finally:
        _ENGINE_DEPTH -= 1


__all__ = ["run_v1"]

# Ensure any uncaught exception returns a Problem with a traceback
run_v1 = _wrap_exceptions(run_v1)


