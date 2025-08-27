# v2/backend/core/prompt_pipeline/executor/engine.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import os, json, hashlib, traceback, tempfile, shutil, fnmatch
from pathlib import Path
from datetime import datetime

from v2.backend.core.spine.contracts import Artifact

# fetch + build + parse/sanitize/verify
from v2.backend.core.introspect.providers import fetch_v1 as introspect_fetch_v1
from v2.backend.core.docstrings.providers import build_prompts_v1 as doc_prompts_build_v1
from v2.backend.core.prompt_pipeline.executor.providers import unpack_results_v1 as results_unpack_v1
from v2.backend.core.prompt_pipeline.llm.response_parser import parse_json_response
from v2.backend.core.docstrings.providers import (
    sanitize_outputs_v1 as docstrings_sanitize_v1,
    verify_batch_v1 as docstrings_verify_v1,
)

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
        self.payload = payload
        self.envelope = {}
        self.payload_schema = {}

    def __getitem__(self, k):
        return self.payload[k]

    def get(self, k, d=None):
        return self.payload.get(k, d)


def _normalize_engine_payload(payload: dict) -> dict:
    """
    Accept old/new payload shapes and guarantee:
      - payload['root']     : repo/project root dir (defaults to CWD if absent)
      - payload['out_file'] : absolute path to engine output JSON file when 'out_base' is provided
    """
    p = dict(payload)

    # Root aliases (fallback to current working directory)
    if not p.get("root"):
        p["root"] = p.get("project_root") or p.get("repo_root") or p.get("root_dir") or os.getcwd()

    # out_file synthesis from out_base (if caller uses newer contract)
    if not p.get("out_file"):
        out_base = p.get("out_base") or p.get("out_dir") or p.get("out_root")
        if out_base:
            ob = Path(out_base).expanduser().resolve()
            ob.mkdir(parents=True, exist_ok=True)
            p["out_file"] = str(ob / "engine.out.json")

    # IMPORTANT: do NOT raise here. Old guard removed.
    return p


def _resolve_out_base(p: dict) -> str:
    # Prefer explicit out_base; otherwise derive from out_file's parent
    val = p.get("out_base") or Path(p["out_file"]).parent.as_posix()
    return str(val).strip()


def _ok(meta: Dict[str, Any]) -> List[Artifact]:
    return [
        Artifact(
            kind="Result",
            uri="spine://result/llm.engine.run.v1",
            sha256="",
            meta=meta,
        )
    ]


def _ng(code: str, message: str, *, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [
        Artifact(
            kind="Problem",
            uri="spine://problem/llm.engine.run.v1",
            sha256="",
            meta={
                "problem": {
                    "code": code,
                    "message": message,
                    "retryable": False,
                    "details": details or {},
                }
            },
        )
    ]


def _problem_art_from_exception(e: BaseException, where: str) -> List[Artifact]:
    tb = traceback.format_exc()
    return _ng(
        e.__class__.__name__,
        f"{where}: {e}",
        details={"where": where, "traceback": tb},
    )


def _as_bool(x: Any, default=False) -> bool:
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


def _call_provider(fn, payload: Dict[str, Any], context: Dict[str, Any], where: str) -> Any:
    """
    Safe provider invoker:
      - supports (task, context) and (task) signatures
      - catches ALL exceptions and returns a Problem-like artifact list
    """
    t = _TaskShim(payload)
    try:
        return fn(t, context or {})
    except TypeError:
        try:
            return fn(t)
        except Exception as e:
            return _problem_art_from_exception(e, where)
    except Exception as e:
        return _problem_art_from_exception(e, where)


def _first_artifact(res: Any) -> Tuple[Optional[Artifact], Dict[str, Any]]:
    if isinstance(res, list) and res:
        a = res[0]
        if isinstance(a, Artifact):
            return a, dict(a.meta or {})
        if isinstance(a, dict):
            return Artifact(kind="Result", uri="spine://shim", sha256="", meta=a), dict(a)
        return None, {}
    if isinstance(res, dict):
        return Artifact(kind="Result", uri="spine://shim", sha256="", meta=res), dict(res)
    return None, {}


def _meta_pick(meta: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in meta and meta[k] is not None:
            return meta[k]
    return default


def _write_manifest_monolith(manifest_path: Path, items: List[Dict[str, Any]]) -> int:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as f:
        for it in items:
            rec = dict(it)
            if "record_type" not in rec:
                rec["record_type"] = "file"
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        return manifest_path.stat().st_size
    except Exception:
        return 0


def _write_manifest_chunked(
    parts_dir: Path,
    parts_index: Path,
    items: List[Dict[str, Any]],
    split_bytes: int,
) -> Dict[str, Any]:
    parts_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for it in items:
        rec = dict(it)
        if "record_type" not in rec:
            rec["record_type"] = "file"
        lines.append(json.dumps(rec, ensure_ascii=False) + "\n")

    parts: List[Dict[str, Any]] = []
    buf: List[str] = []
    buf_bytes = 0
    part_idx = 0

    def flush():
        nonlocal buf, buf_bytes, part_idx
        if not buf:
            return
        name = f"{part_idx:02d}.txt"
        p = parts_dir / name
        with p.open("w", encoding="utf-8") as f:
            for s in buf:
                f.write(s)
        parts.append({"name": name, "size": p.stat().st_size, "lines": len(buf)})
        part_idx += 1
        buf = []
        buf_bytes = 0

    for s in lines:
        sz = len(s.encode("utf-8"))
        if buf and buf_bytes + sz > split_bytes:
            flush()
        buf.append(s)
        buf_bytes += sz
    flush()

    idx = {
        "record_type": "parts_index",
        "dir": parts_dir.name,
        "total_parts": len(parts),
        "split_bytes": split_bytes,
        "parts": parts,
    }
    parts_index.parent.mkdir(parents=True, exist_ok=True)
    parts_index.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    return idx


def _attach_paths_for_patch(
    outputs: List[Dict[str, Any]],
    source_records: List[Dict[str, Any]],
    prepared_batch: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Ensure each patch output has 'relpath' (and 'path' alias) so the patch engine
    knows which file to modify. We try (in order):
      - prepared_batch[idx].relpath|path
      - source_records[idx].relpath|path|filepath|file
    We keep list order to preserve index alignment with batches and results.
    """
    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(outputs):
        rec = dict(item) if isinstance(item, dict) else {}
        has_path = any(k in rec and rec[k] for k in ("relpath", "path"))
        if not has_path:
            src_pre = prepared_batch[idx] if idx < len(prepared_batch) else {}
            src_inp = source_records[idx] if idx < len(source_records) else {}
            rel = (
                (src_pre.get("relpath") or src_pre.get("path"))
                or (src_inp.get("relpath") or src_inp.get("path"))
                or (src_inp.get("filepath") or src_inp.get("file"))
                or ""
            )
            if rel:
                rec.setdefault("relpath", rel)
                rec.setdefault("path", rel)
        out.append(rec)
    return out


def _dir_is_empty(p: Path) -> bool:
    try:
        next(p.iterdir())
        return False
    except StopIteration:
        return True
    except FileNotFoundError:
        return True


def _copy_seed(src: Path, dst: Path, patterns: List[str]) -> None:
    """
    Copy 'src' into 'dst' respecting ignore patterns; does NOT delete extras in dst.
    """
    src = src.resolve()
    dst = dst.resolve()
    dst.mkdir(parents=True, exist_ok=True)

    def _ignore(dirpath, names):
        skipped = []
        for n in names:
            full = Path(dirpath) / n
            rel = str(full.relative_to(src)).replace("\\", "/")
            # match against any-depth patterns and top-level dir matches
            for pat in patterns:
                if fnmatch.fnmatch(rel, pat) or rel.split("/", 1)[0] == pat:
                    skipped.append(n)
                    break
        return set(skipped)

    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=_ignore)


_ENGINE_DEPTH = 0


def run_v1(task: _TaskShim, context: Dict[str, Any]) -> List[Artifact]:
    print("[llm.engine.run.v1] hardened engine ACTIVE (bundle attach enabled)")
    print(
        "[ENGINE DEBUG]",
        "file=", Path(__file__).resolve(),
        "has_norm=", "_normalize_engine_payload" in globals(),
        "keys=", list((task.payload or {}).keys())
    )

    # Normalize payload to accept old/new shapes
    p = _normalize_engine_payload(dict(task.payload or {}))
    print("[ENGINE NORMALIZED]", "root=", p.get("root"), "out_file=", p.get("out_file"), "out_base=", p.get("out_base"))
    ctx = dict(context or {})

    # Defaults
    try:
        from v2.backend.core.configuration.loader import get_llm

        llm_cfg = get_llm()
        _default_provider = str(getattr(llm_cfg, "provider", "") or getattr(llm_cfg, "name", "") or "")
        _default_model = str(getattr(llm_cfg, "model", "") or "")
    except Exception:
        _default_provider = ""
        _default_model = ""

    try:
        from v2.backend.core.configuration.loader import get_db

        db_cfg = get_db()
        _default_sqlalchemy_url = str(getattr(db_cfg, "sqlalchemy_url", "") or "")
        _default_sqlalchemy_table = str(
            getattr(db_cfg, "sqlalchemy_table", "") or getattr(db_cfg, "table", "") or ""
        )
    except Exception:
        _default_sqlalchemy_url = ""
        _default_sqlalchemy_table = ""

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
    out_base = _resolve_out_base(p)

    if not sqlalchemy_url or not sqlalchemy_table:
        return _ng("InvalidPayload", "Missing sqlalchemy_url/sqlalchemy_table")
    if not provider or not model:
        return _ng("InvalidPayload", "Missing provider/model")
    if not out_base:
        return _ng("InvalidPayload", "Missing out_base")

    status_filter = str(p.get("status_filter") or "")
    max_rows = int(p.get("max_rows") or 200)
    exclude_globs = list(p.get("exclude_globs") or [])
    segment_excludes = list(p.get("segment_excludes") or [])
    ask_spec = dict(p.get("ask_spec") or {})

    run_fetch = _as_bool(p.get("run_fetch_targets"), True)
    run_build = _as_bool(p.get("run_build_prompts"), True)
    run_llm = _as_bool(p.get("run_run_llm"), True)
    run_unpack = _as_bool(p.get("run_unpack"), True)
    run_sanitize = _as_bool(p.get("run_sanitize"), True)
    run_verify = _as_bool(p.get("run_verify"), True)
    run_save_patch = _as_bool(p.get("run_save_patch"), True)

    global _ENGINE_DEPTH
    _ENGINE_DEPTH += 1
    try:
        # FETCH
        print("[PHASE] FETCH")
        if run_fetch:
            res = _call_provider(
                introspect_fetch_v1,
                {
                    "sqlalchemy_url": sqlalchemy_url,
                    "sqlalchemy_table": sqlalchemy_table,
                    "status": status_filter,
                    "status_filter": status_filter,
                    "max_rows": max_rows,
                    "exclude_globs": exclude_globs,
                    "segment_excludes": segment_excludes,
                },
                ctx,
                where="introspect.fetch.v1",
            )
            art, meta = _first_artifact(res)
            if not art:
                return _ng("ProviderError", "introspect.fetch.v1 returned nothing")
            if art.kind == "Problem":
                prob = (meta or {}).get("problem", {})
                return _ng(prob.get("code", "ProviderProblem"), prob.get("message", "fetch failed"), details=prob)
            items = _ensure_items(_meta_pick(meta, "items", "result", default=[]))
            if not items:
                return _ng("ValidationError", "No valid targets found.")
        else:
            items = _ensure_items(p.get("items"))
            if not items:
                return _ng("InvalidPayload", "items required when run_fetch_targets=false")

        # BUILD
        print("[PHASE] BUILD")
        if run_build:
            res = _call_provider(
                doc_prompts_build_v1,
                {
                    "records": items,
                    "project_root": os.getcwd(),
                    "context_half_window": int(p.get("context_half_window", 25)),
                    "description_field": str(p.get("description_field", "description")),
                },
                ctx,
                where="docstrings.prompts.build.v1",
            )
            art, meta = _first_artifact(res)
            if not art:
                return _ng("ProviderError", "docstrings.prompts.build.v1 returned nothing")
            if art.kind == "Problem":
                prob = (meta or {}).get("problem", {})
                return _ng(
                    prob.get("code", "ProviderProblem"),
                    prob.get("message", "build prompts failed"),
                    details=prob,
                )
            res_obj = _meta_pick(meta, "result", default={}) or {}
            messages = dict(res_obj.get("messages") or {})
            prepared_batch = list(res_obj.get("batch") or [])
            if not messages.get("user"):
                return _ng("ValidationError", "Empty user prompt")
        else:
            messages = dict(p.get("messages") or {})
            prepared_batch = list(p.get("prepared_batch") or [])
            if not messages.get("user"):
                return _ng("InvalidPayload", "messages required when run_build_prompts=false")

        # PREPARE RUN DIR
        print("[PHASE] PREPARE RUN DIR")
        rd = RunDirs(Path(out_base))
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        rd_obj = rd.ensure(run_id)
        run_root = Path(rd_obj.root)

        bundle_root = run_root / "bundle"
        bundle_root.mkdir(parents=True, exist_ok=True)

        # CODE BUNDLE (make → inject), with robust fallbacks
        print("[PHASE] BUNDLE.MAKE")
        bundle: Dict[str, Any] = {}

        if include_manifest:
            # try NEW API (preferred: out_base)
            res = _call_provider(
                bundle_make_v1,
                {
                    "mode": code_bundle_mode,
                    "publish_github": publish_github,
                    "chunk_manifest": chunk_manifest,
                    "split_bytes": split_bytes,
                    "group_dirs": group_dirs,
                    "out_base": out_base,
                    "run_dir": str(run_root),
                    "project_root": os.getcwd(),
                    "exclude_globs": exclude_globs,
                },
                ctx,
                where="packager.bundle.make.NEW",
            )
            art, meta = _first_artifact(res)

            # Detect need for legacy via message or absence of artifact
            need_legacy = False
            if not art:
                need_legacy = True
            elif art.kind == "Problem":
                prob = (meta or {}).get("problem", {}) if isinstance(meta, dict) else {}
                msg = str(prob.get("message", "")).lower()
                if "payload must include 'root' and 'out_file'" in msg or ("root" in msg and "out_file" in msg):
                    need_legacy = True

            if need_legacy:
                print("[PHASE] BUNDLE.MAKE → LEGACY fallback")
                # legacy: requires 'root' and 'out_file'
                manifest_path = bundle_root / "design_manifest.jsonl"
                res2 = _call_provider(
                    bundle_make_v1,
                    {
                        "root": os.getcwd(),
                        "out_file": str(manifest_path),
                        "exclude_globs": exclude_globs,
                    },
                    ctx,
                    where="packager.bundle.make.LEGACY",
                )
                art2, meta2 = _first_artifact(res2)

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
                    print("[PHASE] BUNDLE.MAKE → LOCAL INDEX fallback")
                    # FINAL FALLBACK: build manifest locally via utils_code_index
                    res3 = _call_provider(
                        utils_code_index_v1, {"project_root": os.getcwd(), "exclude_globs": exclude_globs}, ctx, where="utils.code_index"
                    )
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
        print("[PHASE] BUNDLE.INJECT")
        res = _call_provider(
            bundle_inject_prompt_v1,
            {
                "bundle": bundle,
                "messages": messages,
                "ask_spec": ask_spec,
                "prepared_batch": prepared_batch,
                "bundle_meta": {
                    "mode": code_bundle_mode,
                    "chunk_manifest": chunk_manifest,
                    "split_bytes": split_bytes,
                    "group_dirs": group_dirs,
                },
            },
            ctx,
            where="packager.bundle.inject",
        )
        art, meta = _first_artifact(res)
        if not art:
            return _ng("ProviderError", "packager_bundle_inject_prompt.v1 returned nothing")
        if art.kind == "Problem":
            prob = (meta or {}).get("problem", {})
            return _ng(prob.get("code", "ProviderProblem"), prob.get("message", "bundle inject failed"), details=prob)

        bundle = dict(_meta_pick(meta, "result", "bundle", default=bundle) or bundle)

        # LLM
        print("[PHASE] LLM")
        if run_llm:
            batches = []
            for idx, it in enumerate(items):
                batches.append(
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
                "batches": batches,
                "ask_spec": ask_spec,
                "bundle": bundle,
            }
            res = _call_provider(llm_complete_batches_v1, llm_payload, ctx, where="llm.complete_batches.v1")
            art, meta = _first_artifact(res)
            if not art:
                return _ng("ProviderError", "llm.complete_batches.v1 returned nothing")
            if art.kind == "Problem":
                prob = (meta or {}).get("problem", {})
                return _ng(prob.get("code", "ProviderProblem"), prob.get("message", "LLM failed"), details=prob)

            raw_results = _meta_pick(meta, "results", "result", default=[])
            results_list = list(raw_results) if isinstance(raw_results, list) else []
            if not results_list:
                return _ng("ValidationError", "LLM returned no results")
        else:
            results_list = list(p.get("results") or [])

        # UNPACK → SANITIZE → VERIFY
        print("[PHASE] UNPACK/SANITIZE/VERIFY")
        up_res = _call_provider(
            results_unpack_v1,
            {"results": results_list, "results_map": {i: v for i, v in enumerate(results_list)}},
            ctx,
            where="results.unpack",
        )
        art, meta = _first_artifact(up_res)
        if not art:
            return _ng("ProviderError", "results.unpack returned nothing")
        if art.kind == "Problem":
            prob = (meta or {}).get("problem", {})
            return _ng(prob.get("code", "ProviderProblem"), prob.get("message", "unpack failed"), details=prob)

        parsed_items = list(_meta_pick(meta, "items", "result", default=[]))

        if run_sanitize:
            san_res = _call_provider(
                docstrings_sanitize_v1,
                {"items": parsed_items, "prepared_items": prepared_batch},
                ctx,
                where="docstrings.sanitize",
            )
            art, meta = _first_artifact(san_res)
            if not art:
                return _ng("ProviderError", "docstrings.sanitize returned nothing")
            if art.kind == "Problem":
                prob = (meta or {}).get("problem", {})
                return _ng(prob.get("code", "ProviderProblem"), prob.get("message", "sanitize failed"), details=prob)
            sanitized = list(_meta_pick(meta, "items", "result", default=parsed_items))
        else:
            sanitized = parsed_items

        if run_verify:
            ver_res = _call_provider(docstrings_verify_v1, {"items": sanitized}, ctx, where="docstrings.verify")
            art, meta = _first_artifact(ver_res)
            if not art:
                return _ng("ProviderError", "docstrings.verify returned nothing")
            if art.kind == "Problem":
                prob = (meta or {}).get("problem", {})
                return _ng(prob.get("code", "ProviderProblem"), prob.get("message", "verify failed"), details=prob)
            verified = list(_meta_pick(meta, "items", "result", default=sanitized))
        else:
            verified = sanitized

        # Ensure patch items carry a target path
        patched_inputs = _attach_paths_for_patch(verified, items, prepared_batch)
        missing = [i for i, it in enumerate(patched_inputs) if not (it.get("relpath") or it.get("path"))]
        print(f"[PHASE] PATCH.APPLY (attach paths) missing={len(missing)} of {len(patched_inputs)}")

        # SAVE/APPLY (same run_dir)
        patched = _call_provider(
            patch_run_v1,
            {
                "items": patched_inputs,  # use verified+paths
                "out_base": out_base,
                "write": True,
                "dry_run": False,
                "provider": provider,
                "model": model,
                "ask_spec": ask_spec,
                "sqlalchemy_url": sqlalchemy_url,
                "sqlalchemy_table": sqlalchemy_table,
                "llm": {"provider": provider, "model": model, "ask_spec": ask_spec},
                "engine": {"provider": provider, "model": model, "ask_spec": ask_spec},
                "llm_provider": provider,
                "llm_model": model,
                "raw_prompts": messages,
                "raw_responses": results_list,
                "prepared_batch": prepared_batch,
                "verify_summary": {"count": len(verified), "errors": 0},
                "run_dir": str(run_root),
            },
            ctx,
            where="patch_engine.run",
        )
        art, meta = _first_artifact(patched)
        if not art:
            return _ng("ProviderError", "patch_engine.run returned nothing")
        if art.kind == "Problem":
            prob = (meta or {}).get("problem", {})
            return _ng(prob.get("code", "ProviderProblem"), prob.get("message", "patch apply failed"), details=prob)

        result_patched = (meta.get("result") if isinstance(meta, dict) else None) or meta or {}

        # --- Optional: apply saved patches ---
        apply_in_sandbox = _as_bool(p.get("run_apply_patch_sandbox"), False)
        archive_and_replace = _as_bool(p.get("run_archive_and_replace"), False)

        applied_runs: List[Dict[str, Any]] = []
        applied_ok = 0

        if apply_in_sandbox and result_patched.get("patches"):
            try:
                from v2.backend.core.patch_engine.config import PatchEngineConfig
                from v2.backend.core.patch_engine.interactive_run import run_one

                seed_root = Path(p.get("root") or os.getcwd()).resolve()

                patch_target_root = p.get("patch_target_root")
                seed_strategy = str(p.get("patch_seed_strategy") or "once").strip().lower()  # once|always|skip

                if patch_target_root:
                    # --- Fixed target mode (apply into v3) ---
                    target_root = Path(patch_target_root).expanduser().resolve()

                    # Determine ignores
                    ignore_globs = [
                        "output", "dist", "build", "__pycache__", ".venv", "venv", ".git", ".idea", "*.log", "*.tmp"
                    ]

                    # Seed according to strategy
                    need_seed = False
                    if seed_strategy == "always":
                        need_seed = True
                    elif seed_strategy == "once":
                        need_seed = (not target_root.exists()) or _dir_is_empty(target_root)

                    if need_seed:
                        print(f"[PATCH.APPLY] seeding target '{target_root}' from '{seed_root}' (strategy={seed_strategy})")
                        _copy_seed(seed_root, target_root, ignore_globs)
                    else:
                        print(f"[PATCH.APPLY] seed skipped (strategy={seed_strategy}, target exists) target='{target_root}'")

                    # Choose source_seed_dir for the applier:
                    # - to avoid re-copy, give it an empty temp dir when we don't want seeding this run
                    if seed_strategy in {"skip"} or (seed_strategy == "once" and not need_seed):
                        empty_src = Path(tempfile.mkdtemp(prefix="pe_seed_empty_"))
                        src_for_applier = empty_src
                    elif seed_strategy == "always" or (seed_strategy == "once" and need_seed):
                        src_for_applier = seed_root
                    else:
                        src_for_applier = seed_root  # safe default

                    cfg = PatchEngineConfig(
                        mirror_current=target_root,
                        source_seed_dir=src_for_applier,
                        initial_tests=[],
                        extensive_tests=[],
                        archive_enabled=archive_and_replace,
                        promotion_enabled=False,
                    )
                    cfg.ensure_dirs()

                    for entry in result_patched["patches"]:
                        patch_path = Path(entry.get("patch")).resolve()
                        if not patch_path.exists():
                            continue
                        manifest = run_one(patch_path, cfg)
                        outcome = getattr(manifest, "data", {}).get("outcome", {}) if hasattr(manifest, "data") else {}
                        status = (outcome or {}).get("status") or ""
                        if status in {"promoted", "would_promote_but_disabled"}:
                            applied_ok += 1
                        applied_runs.append({
                            "patch": entry.get("patch"),
                            "target_root": str(target_root),
                            "status": status,
                        })
                else:
                    # --- Ephemeral mirror mode (default; mirror outside repo) ---
                    mirror_base = Path(
                        p.get("patch_mirror_root")
                        or os.environ.get("PATCH_MIRROR_ROOT")
                        or tempfile.gettempdir()
                    ).resolve()

                    run_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
                    current_mirror = (mirror_base / "pm" / run_tag / "cur").resolve()

                    # Safety: if mirror would live inside the repo, force temp-based mirror to avoid recursion
                    if str(current_mirror).lower().startswith(str(seed_root).lower()):
                        mirror_base = Path(tempfile.gettempdir()).resolve()
                        current_mirror = (mirror_base / "pm" / run_tag / "cur").resolve()

                    current_mirror.mkdir(parents=True, exist_ok=True)
                    print(f"[PATCH.APPLY] mirror={current_mirror}")

                    cfg = PatchEngineConfig(
                        mirror_current=current_mirror,
                        source_seed_dir=seed_root,
                        initial_tests=[],
                        extensive_tests=[],
                        archive_enabled=archive_and_replace,
                        promotion_enabled=False,
                    )
                    cfg.ensure_dirs()

                    for entry in result_patched["patches"]:
                        patch_path = Path(entry.get("patch")).resolve()
                        if not patch_path.exists():
                            continue
                        manifest = run_one(patch_path, cfg)
                        outcome = getattr(manifest, "data", {}).get("outcome", {}) if hasattr(manifest, "data") else {}
                        status = (outcome or {}).get("status") or ""
                        if status in {"promoted", "would_promote_but_disabled"}:
                            applied_ok += 1
                        applied_runs.append({
                            "patch": entry.get("patch"),
                            "mirror": str(current_mirror),
                            "status": status,
                        })

            except Exception as e:
                applied_runs.append({"error": f"{type(e).__name__}: {e}"})

            # attach apply runs for visibility
            result_patched["apply_runs"] = applied_runs

        return _ok(
            {
                "stats": {
                    "fetched": len(items),
                    "built": len(items),
                    "completed": len(results_list),
                    "unpacked": len(parsed_items),
                    "sanitize_errors": 0,
                    "verify_errors": 0,
                    "patches_saved": len(result_patched.get("patches") or []),
                    "patches_applied": applied_ok if apply_in_sandbox else 0,
                    "archived": 0,
                    "rolled_back": 0,
                    "model": model,
                    "provider": provider,
                    "table": sqlalchemy_table,
                    "status_filter": status_filter,
                    "out_base": out_base,
                    "code_bundle_mode": code_bundle_mode,
                    "chunk_manifest": chunk_manifest,
                    "split_bytes": split_bytes,
                    "group_dirs": group_dirs,
                },
                "bundle": bundle,
                "patched": result_patched,
            }
        )
    except Exception as e:
        # Absolute last-resort catch so uncaught provider errors never bubble
        return _problem_art_from_exception(e, "engine.run_v1")
    finally:
        _ENGINE_DEPTH -= 1


__all__ = ["run_v1"]




