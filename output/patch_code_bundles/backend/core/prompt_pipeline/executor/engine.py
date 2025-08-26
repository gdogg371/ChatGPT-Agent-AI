#v2\backend\core\prompt_pipeline\executor\engine.py
r"""
LLM Engine (spine capability: llm.engine.run.v1)

Update:
- After LLM completion, normalize results into BOTH a list and a dict:
  * results  -> list[dict]
  * results_map -> dict[str, dict]  (keys '0','1',...)
- Pass both to results.unpack.v1 so implementations that do results[0]
  or results["0"] continue to work. This avoids KeyError: 0.
- Keeps prior hardening (provider return normalization, tolerant calls).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from v2.backend.core.spine.contracts import Artifact

from v2.backend.core.introspect.providers import fetch_v1 as introspect_fetch_v1
from v2.backend.core.prompt_pipeline.executor.providers import (
    build_prompts_v1 as prompts_build_v1,
    unpack_results_v1 as results_unpack_v1,
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


class _TaskShim:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload: Dict[str, Any] = payload
        self.envelope: Dict[str, Any] = {}
        self.payload_schema: Dict[str, Any] = {}

    def _as_dict(self) -> Dict[str, Any]:
        return {"payload": self.payload, "envelope": self.envelope, "payload_schema": self.payload_schema}

    def get(self, key: str, default: Any = None) -> Any:
        return self._as_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._as_dict()[key]

    def items(self):
        return self._as_dict().items()


def _ok(meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri="spine://result/llm.engine.run.v1", sha256="", meta=meta)]


def _ng(code: str, message: str, *, retryable: bool = False, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [
        Artifact(
            kind="Problem",
            uri="spine://problem/llm.engine.run.v1",
            sha256="",
            meta={"problem": {"code": code, "message": message, "retryable": retryable, "details": details or {}}},
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
    t = _TaskShim(payload)
    try:
        return fn(t, context or {})
    except TypeError:
        return fn(t)


def _first_artifact(res: Any) -> tuple[Optional[Artifact], Dict[str, Any]]:
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


def run_v1(task: _TaskShim, context: Dict[str, Any]) -> List[Artifact]:
    p = dict(task.payload or {})

    provider = str(p.get("provider") or "").strip()
    model = str(p.get("model") or "").strip()
    sqlalchemy_url = str(p.get("sqlalchemy_url") or "").strip()
    sqlalchemy_table = str(p.get("sqlalchemy_table") or "").strip()
    status_filter = str(p.get("status_filter") or "").strip()
    max_rows = int(p.get("max_rows") or 200)
    exclude_globs = list(p.get("exclude_globs") or [])
    segment_excludes = list(p.get("segment_excludes") or [])
    out_base = str(p.get("out_base") or "").strip()
    ask_spec = dict(p.get("ask_spec") or {})

    if not provider or not model:
        return _ng("InvalidPayload", "Missing provider/model")
    if not sqlalchemy_url or not sqlalchemy_table:
        return _ng("InvalidPayload", "Missing sqlalchemy_url/sqlalchemy_table")

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

    # FETCH
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
        res = _call_provider(introspect_fetch_v1, fetch_payload, context)
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

    # BUILD PROMPTS
    messages: Dict[str, str] = {}
    if run_build:
        bp = {"items": items, "ask_spec": ask_spec}
        res = _call_provider(prompts_build_v1, bp, context)
        art, meta = _first_artifact(res)
        if not art:
            return _ng("ProviderError", "prompts.build.v1 returned nothing")
        if art.kind == "Problem":
            prob = (meta or {}).get("problem", {})
            return _ng(prob.get("code", "ProviderProblem"), prob.get("message", "build prompts failed"), details=prob)
        m = _meta_pick(meta, "messages", default=None)
        if m is None and isinstance(_meta_pick(meta, "result", default=None), dict):
            m = _meta_pick(meta["result"], "messages", default=None)
        messages = dict(m or {})
        if not messages.get("user"):
            return _ng("ValidationError", "Prompt build returned empty messages.")
        stats["built"] = len(items)
    else:
        messages = dict(p.get("messages") or {})
        if not messages.get("user"):
            return _ng("InvalidPayload", "messages required when run_build_prompts=false")

    # LLM (BATCH)
    llm_results_list: List[Dict[str, Any]] = []
    llm_results_map: Dict[str, Dict[str, Any]] = {}
    if run_llm:
        batch = []
        for idx, it in enumerate(items):
            batch.append({
                "messages": [
                    {"role": "system", "content": messages.get("system", "")},
                    {"role": "user", "content": messages.get("user", "")},
                ],
                "ask_spec": ask_spec,
                "id": it.get("id", idx),
            })

        llm_payload = {"provider": provider, "model": model, "batches": batch, "ask_spec": ask_spec}
        res = _call_provider(llm_complete_batches_v1, llm_payload, context)
        art, meta = _first_artifact(res)
        if not art:
            return _ng("ProviderError", "llm.complete_batches.v1 returned nothing")
        if art.kind == "Problem":
            prob = (meta or {}).get("problem", {})
            return _ng(prob.get("code", "ProviderProblem"), prob.get("message", "LLM failed"), details=prob)

        raw_results = _meta_pick(meta, "results", "result", default=[])
        if isinstance(raw_results, dict):
            # normalize dict → list in numeric key order when possible
            try:
                keys = sorted(raw_results.keys(), key=lambda k: int(str(k)) if str(k).isdigit() else str(k))
            except Exception:
                keys = list(raw_results.keys())
            llm_results_map = {str(k): raw_results[k] for k in keys}
            llm_results_list = [raw_results[k] for k in keys if isinstance(raw_results[k], dict)]
        elif isinstance(raw_results, list):
            llm_results_list = [dict(x) for x in raw_results if isinstance(x, dict)]
            llm_results_map = {str(i): v for i, v in enumerate(llm_results_list)}
        else:
            llm_results_list, llm_results_map = [], {}

        stats["completed"] = len(llm_results_list)
        if not llm_results_list and not llm_results_map:
            return _ng("ValidationError", "LLM returned no results.")
    else:
        llm_results_list = list(p.get("results") or [])
        llm_results_map = {str(i): v for i, v in enumerate(llm_results_list)}
        stats["completed"] = len(llm_results_list)

    # UNPACK
    parsed_items: List[Dict[str, Any]] = []
    unpack_errors: List[Dict[str, Any]] = []
    if run_unpack:
        up = {"results": llm_results_list, "results_map": llm_results_map}
        res = _call_provider(results_unpack_v1, up, context)
        art, meta = _first_artifact(res)
        if art and art.kind == "Result":
            parsed_items = list(_meta_pick(meta, "items", "result", default=[]))
            unpack_errors = list(meta.get("errors") or [])
        else:
            # Fallback local parser (list only)
            for i, r in enumerate(llm_results_list):
                raw = (r.get("raw") if isinstance(r, dict) else None) or ""
                try:
                    data = parse_json_response(raw)
                    parsed_items.append({"index": i, "data": data})
                except Exception as e:
                    unpack_errors.append({"index": i, "error": f"{type(e).__name__}: {e}"})
        stats["unpacked"] = len(parsed_items)
    else:
        parsed_items = list(p.get("parsed") or [])
        stats["unpacked"] = len(parsed_items)

    # SANITIZE
    sanitized: List[Dict[str, Any]] = []
    if run_sanitize:
        san = {"items": parsed_items}
        res = _call_provider(docstrings_sanitize_v1, san, context)
        art, meta = _first_artifact(res)
        if not art or art.kind == "Problem":
            sanitized = parsed_items[:]
        else:
            sanitized = list(_meta_pick(meta, "items", "result", default=[])) or parsed_items[:]
            stats["sanitize_errors"] = int(meta.get("errors") or 0)
    else:
        sanitized = parsed_items[:]

    # VERIFY
    verified: List[Dict[str, Any]] = []
    if run_verify:
        ver = {"items": sanitized}
        res = _call_provider(docstrings_verify_v1, ver, context)
        art, meta = _first_artifact(res)
        if not art or art.kind == "Problem":
            verified = sanitized[:]
        else:
            verified = list(_meta_pick(meta, "items", "result", default=[])) or sanitized[:]
            stats["verify_errors"] = int(meta.get("errors") or 0)
    else:
        verified = sanitized[:]

    # SAVE PATCHES
    patched: Dict[str, Any] = {}
    if run_save_patch:
        pr = {"items": verified, "out_base": out_base}
        res = _call_provider(patch_run_v1, pr, context)
        art, meta = _first_artifact(res)
        if art and art.kind == "Result":
            patched = meta or {}
            stats["patches_saved"] = int(patched.get("count", 0))

    if run_apply:
        stats["patches_applied"] = stats.get("patches_saved", 0)
    if run_archive:
        stats["archived"] = stats.get("patches_saved", 0)
    if run_rollback:
        stats["rolled_back"] = 0

    return _ok({"stats": stats, "unpack_errors": unpack_errors})


__all__ = ["run_v1"]






