# File: v2/backend/core/run/docstrings.py
from __future__ import annotations
import json, sys
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from v2.backend.core.spine.registry import REGISTRY as SPINE_REGISTRY  # type: ignore

def _ensure_dir(p: Path) -> None: p.mkdir(parents=True, exist_ok=True)
def _write_json(p: Path, obj: Any) -> None:
    try:
        _ensure_dir(p.parent)
        p.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def _project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config" / "spine" / "capabilities.yml").exists():
            return parent
    return here.parents[5]

def _load_vars(root: Path) -> Dict[str, Any]:
    vars_path = root / "config" / "spine" / "pipelines" / "default" / "vars.yml"
    with vars_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}

def _now_token() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def _import_callable(target: str):
    if ":" not in target:
        raise ValueError(f"Invalid target '{target}'; expected 'module.path:function'")
    mod_path, fn_name = target.split(":", 1)
    mod = import_module(mod_path)
    return getattr(mod, fn_name)

def _bootstrap_spine(root: Path) -> Dict[str, Any]:
    cap_path = root / "config" / "spine" / "capabilities.yml"
    info: Dict[str, Any] = {"root": str(root), "capabilities_file": str(cap_path), "registered": [], "errors": []}
    if not cap_path.exists():
        info["errors"].append(f"Capabilities file not found: {cap_path}")
        return info
    try:
        data = yaml.safe_load(cap_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        info["errors"].append(f"Failed to read capabilities.yml: {e}")
        return info
    register_fn = getattr(SPINE_REGISTRY, "register", None) or getattr(SPINE_REGISTRY, "add", None)
    if not callable(register_fn):
        info["errors"].append("Registry has no 'register' or 'add' method.")
        return info
    for cap_name, cfg in data.items():
        try:
            target = (cfg or {}).get("target")
            if not target:
                continue
            fn = _import_callable(target)
            try:
                register_fn(cap_name, fn)
            except TypeError:
                register_fn(cap_name, fn, input_schema=cfg.get("input_schema"), output_schema=cfg.get("output_schema"))
            info["registered"].append(cap_name)
        except Exception as e:
            info["errors"].append(f"{cap_name}: {e}")
    return info

def _arts_list(x: Any) -> List[Dict[str, Any]]:
    if x is None: return []
    if isinstance(x, list):
        out: List[Dict[str, Any]] = []
        for a in x:
            if isinstance(a, dict):
                out.append({"kind": a.get("kind"), "uri": a.get("uri"), "sha256": a.get("sha256",""), "meta": a.get("meta")})
            else:
                out.append({"kind": getattr(a,"kind",None), "uri": getattr(a,"uri",None),
                            "sha256": getattr(a,"sha256","") or "", "meta": getattr(a,"meta",None)})
        return out
    if isinstance(x, dict): return [x]
    return []

def _unwrap_meta(x: Any) -> Dict[str, Any]:
    arts = _arts_list(x)
    if arts:
        m = arts[0].get("meta")
        if isinstance(m, dict): return m
    return dict(x) if isinstance(x, dict) else {}

def _ensure_list_of_dicts(x: Any) -> List[Dict[str, Any]]:
    if isinstance(x, list): return [r for r in x if isinstance(r, dict)]
    return []

def _get(meta: Dict[str, Any], *path: str, default: Any = None) -> Any:
    cur: Any = meta
    for key in path:
        if not isinstance(cur, dict): return default
        cur = cur.get(key)
    return default if cur is None else cur

def _get_items(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = meta.get("items")
    if not isinstance(items, list): items = _get(meta, "result", "items", default=[])
    return _ensure_list_of_dicts(items)

def _get_messages_and_batch(meta: Dict[str, Any]) -> tuple[list[dict], list[dict]]:
    messages = meta.get("messages")
    if not isinstance(messages, list): messages = _get(meta, "result", "messages", default=[])
    batch = meta.get("batch")
    if not isinstance(batch, list): batch = _get(meta, "result", "batch", default=[])
    return _ensure_list_of_dicts(messages), _ensure_list_of_dicts(batch)

def _spine_run(capability: str, payload: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Any:
    try:
        return SPINE_REGISTRY.run(capability, payload, context or {})
    except KeyError as e:
        return [{"kind":"Problem","uri":f"spine://capability/{capability}",
                 "meta":{"problem":{"code":"CapabilityNotFound","message":str(e),"retryable":False,"details":{}}}}]
    except Exception as e:
        return [{"kind":"Problem","uri":f"spine://capability/{capability}",
                 "meta":{"problem":{"code":"CapabilityError","message":str(e),"retryable":False,"details":{}}}}]

def _normalize_record(r: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(r)
    if "filepath" not in out and "file" in out: out["filepath"] = out.get("file")
    if "lineno" not in out and "line" in out: out["lineno"] = out.get("line")
    if "symbol_type" not in out and "filetype" in out: out["symbol_type"] = out.get("filetype")
    return out

def _normalize_records(rs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_normalize_record(r) for r in rs]

def _normalize_enriched_item(i: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(i)
    if "filepath" not in out and "path" in out: out["filepath"] = out["path"]
    if "lineno" not in out and "line" in out: out["lineno"] = out["line"]
    return out

def _normalize_enriched(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_normalize_enriched_item(i) for i in items]

def main(argv: Optional[List[str]] = None) -> int:
    root = _project_root()
    vars = _load_vars(root)
    out_base = Path(vars.get("out_base") or "output/patches_received")
    run_dir = out_base / _now_token()
    _ensure_dir(run_dir)

    print("[llm.engine.run.v1] engine ACTIVE (bundle logging + prompt inject via Spine)")
    boot = _bootstrap_spine(root)
    _write_json(run_dir / ("spine.bootstrap.errors.json" if boot.get("errors") else "spine.bootstrap.json"), boot)

    print("[PHASE] FETCH")
    fetch_payload = {
        "sqlalchemy_url": vars.get("sqlalchemy_url"),
        "sqlalchemy_table": vars.get("sqlalchemy_table", "introspection_index"),
        "status": vars.get("status", "todo"),
        "status_any": vars.get("status_any"),
        "max_rows": int(vars.get("max_rows", 3)),
        "exclude_globs": vars.get("exclude_globs") or [],
        "segment_excludes": vars.get("segment_excludes") or [],
    }
    fetch_res = _spine_run("introspect.fetch.v1", fetch_payload, {"phase": "FETCH"})
    fetch_meta = _unwrap_meta(fetch_res)
    raw_records = _ensure_list_of_dicts(fetch_meta.get("items") or fetch_meta.get("records") or _get(fetch_meta, "result", "items", default=[]))
    _write_json(run_dir / "fetch.meta.json", {"meta": fetch_meta, "sample_first_3": raw_records[:3]})
    records = _normalize_records(raw_records)
    print(f"[FETCH] records={len(records)}")
    _write_json(run_dir / "fetch.normalized.sample.json", records[:3])

    if not records:
        final = {
            "run_dir": str(run_dir),
            "counts": {"built_messages": 0, "built_batch": 0, "sanitized": 0, "verified": 0},
            "problems": [{
                "phase": "FETCH","capability": "introspect.fetch.v1","kind":"Problem","uri":"spine://capability/introspect.fetch.v1",
                "problem":{"code":"NoRecords","message":"Fetch returned zero rows; cannot proceed.","retryable":False,
                           "details":{k: fetch_payload.get(k) for k in ("sqlalchemy_table","status","status_any","max_rows")}},
                "meta": fetch_meta
            }]
        }
        print(json.dumps(final, indent=2, ensure_ascii=False)); return 0

    print("[PHASE] ENRICH")
    enr_res = _spine_run("retriever.enrich.v1",
                         {"records": records, "project_root": str(root), "exclude_globs": vars.get("exclude_globs") or []},
                         {"phase": "ENRICH"})
    enr_meta = _unwrap_meta(enr_res)
    items_raw = _get_items(enr_meta)
    items = _normalize_enriched(items_raw)
    _write_json(run_dir / "enrich.meta.json", {"meta": enr_meta, "count": len(items), "sample_first_3": items[:3]})
    print(f"[ENRICH] items={len(items)}")
    if len(items) == 0:
        final = {
            "run_dir": str(run_dir),
            "counts": {"built_messages": 0, "built_batch": 0, "sanitized": 0, "verified": 0},
            "problems": [{
                "phase":"ENRICH","capability":"retriever.enrich.v1","kind":"Problem","uri":"spine://capability/retriever.enrich.v1",
                "problem":{"code":"EnrichReturnedZero","message":"Enricher returned 0 items. Check field mapping and excludes.",
                           "retryable":False,"details":{"received_record_keys":sorted({k for r in records[:5] for k in r.keys()}),
                                                        "exclude_globs": vars.get("exclude_globs") or [],
                                                        "segment_excludes": vars.get("segment_excludes") or []}},
                "meta": enr_meta
            }]
        }
        print(json.dumps(final, indent=2, ensure_ascii=False)); return 0

    print("[PHASE] CONTEXT.BUILD")
    # (Optional context phase)

    print("[PHASE] BUILD")
    build_res = _spine_run("prompts.build.v1",
                           {"items": items, "ask_spec": vars.get("ask_spec") or {},
                            "exclude_globs": vars.get("exclude_globs") or [],
                            "segment_excludes": vars.get("segment_excludes") or []},
                           {"phase": "BUILD"})
    build_meta = _unwrap_meta(build_res)
    messages, batch = _get_messages_and_batch(build_meta)
    _write_json(run_dir / "build.meta.json", {"meta": build_meta, "messages_count": len(messages), "batch_count": len(batch)})
    built_messages = 1 if (items and (messages or batch)) else 0
    built_batch = len(batch)

    print("[PHASE] BUNDLE.INJECT")
    inj_res = _spine_run("packager.bundle.inject_prompt.v1", {"items": items}, {"phase": "BUNDLE.INJECT"})
    _write_json(run_dir / "bundle.inject.meta.json", _unwrap_meta(inj_res))

    print("[PHASE] LLM")
    provider, model = vars.get("provider"), vars.get("model")
    ask_spec = vars.get("ask_spec") or {}
    if built_batch > 0:
        llm_res = _spine_run("llm.complete_batches.v1",
                             {"provider": provider, "model": model, "batches": batch,
                              "ask_spec": ask_spec, "run_dir": str(run_dir)},
                             {"phase": "LLM"})
    else:
        llm_res = _spine_run("llm.complete.v1",
                             {"provider": provider, "model": model, "messages": messages,
                              "ask_spec": ask_spec, "run_dir": str(run_dir)},
                             {"phase": "LLM"})

    print("[PHASE] LLM.UNPACK")
    # >>> FIX: wrap results in a dict so the provider can .update() and coerce
    llm_payload = {"results": llm_res} if isinstance(llm_res, list) else (llm_res or {})
    unpack = _spine_run("results.unpack.v1", llm_payload, {"phase": "LLM.UNPACK"})
    unpack_meta = _unwrap_meta(unpack)
    llm_items = _get_items(unpack_meta)
    _write_json(run_dir / "llm.unpack.meta.json", {"meta": unpack_meta, "items_count": len(llm_items)})

    print("[PHASE] SANITIZE")
    san = _spine_run("sanitize.v1", {"items": llm_items}, {"phase": "SANITIZE"})
    san_meta = _unwrap_meta(san)
    sanitized = _get_items(san_meta)
    _write_json(run_dir / "sanitize.meta.json", {"meta": san_meta, "items_count": len(sanitized)})
    print(f"[SANITIZE] items={len(sanitized)}")

    print("[PHASE] VERIFY")
    ver = _spine_run("verify.v1", {"items": sanitized}, {"phase": "VERIFY"})
    ver_meta = _unwrap_meta(ver)
    ok_items = _get_items(ver_meta)
    _write_json(run_dir / "verify.meta.json", {"meta": ver_meta, "ok_count": len(ok_items)})
    print(f"[VERIFY] ok_items={len(ok_items)}")

    print("[PHASE] PATCH.APPLY_FILES")
    final = {
        "run_dir": str(run_dir),
        "counts": {"built_messages": int(built_messages), "built_batch": int(built_batch),
                   "sanitized": int(len(sanitized)), "verified": int(len(ok_items))},
        "problems": []
    }
    print(json.dumps(final, indent=2, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))



