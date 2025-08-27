# v2/backend/core/patch_engine/providers.py
from __future__ import annotations

from typing import Any, Dict, List
import os
import json
import re
from datetime import datetime
import dataclasses

from v2.backend.core.spine.contracts import Artifact, Task


def _ok(result: Any, *, kind: str = "Result", uri: str = "spine://patch/ok") -> List[Artifact]:
    return [Artifact(kind=kind, uri=uri, sha256="", meta={"result": result})]


def _err(code: str, message: str, details: Dict[str, Any] | None = None, *, uri: str = "spine://patch/error") -> List[Artifact]:
    return [
        Artifact(
            kind="Problem",
            uri=uri,
            sha256="",
            meta={
                "problem": {
                    "code": code,
                    "message": message,
                    "retryable": False,
                    "details": dict(details or {}),
                }
            },
        )
    ]


# -------------------- helpers --------------------

def _slug(x: Any) -> str:
    s = str("" if x is None else x)
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s)
    return s or "item"


def _jsonify(x: Any) -> Any:
    # primitives
    if x is None or isinstance(x, (bool, int, float, str)):
        return x
    # dict-like
    if isinstance(x, dict):
        return {str(k): _jsonify(v) for k, v in x.items()}
    # list/tuple/set
    if isinstance(x, (list, tuple, set)):
        return [_jsonify(v) for v in x]
    # dataclasses
    if dataclasses.is_dataclass(x):
        try:
            return _jsonify(dataclasses.asdict(x))
        except Exception:
            pass
    # pydantic v1/v2
    for m in ("model_dump", "dict"):
        if hasattr(x, m) and callable(getattr(x, m)):
            try:
                return _jsonify(getattr(x, m)())
            except Exception:
                pass
    # namedtuple / _asdict
    if hasattr(x, "_asdict") and callable(getattr(x, "_asdict")):
        try:
            return _jsonify(x._asdict())
        except Exception:
            pass
    # plain objects
    if hasattr(x, "__dict__"):
        try:
            return _jsonify(vars(x))
        except Exception:
            pass
    # fallback: string representation
    try:
        return repr(x)
    except Exception:
        return "<unserializable>"


# -------------------- capability: write patches --------------------

def apply_files_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Spine capability: patch.apply_files.v1

    Writes patch intents. If the caller doesn't pass run_dir/run_id,
    a NEW timestamped run directory under out_base is created.

    Expected payload fields:
      - out_base: str (e.g., "output/patches_test")  [required]
      - items: list[Any] (parsed/verified patch intents)  [can be empty]
      - run_dir (optional): explicit run folder under out_base
      - run_id  (optional): name for a new run folder
    """
    p: Dict[str, Any] = dict(getattr(task, "payload", {}) or {})
    out_base: str = str(p.get("out_base") or "").strip()
    raw_items = p.get("items", [])

    if not out_base:
        return _err("InvalidPayload", "Missing out_base", uri="spine://problem/patch.apply_files.v1")

    # normalize items
    if isinstance(raw_items, list):
        items: List[Any] = raw_items
    elif raw_items is None:
        items = []
    else:
        items = [raw_items]

    # always prefer a fresh run dir unless explicitly provided
    run_dir: str = str(p.get("run_dir") or "").strip()
    if not run_dir:
        run_id = str(p.get("run_id") or "").strip() or datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = os.path.join(out_base, run_id)

    patches_dir = os.path.join(run_dir, "patches")
    os.makedirs(patches_dir, exist_ok=True)

    saved = 0
    written: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for idx, it in enumerate(items):
        try:
            if isinstance(it, dict) and isinstance(it.get("patches"), list) and it["patches"]:
                for j, sub in enumerate(it["patches"]):
                    data = _jsonify(sub)
                    base = _slug(it.get("id", idx))
                    fname = f"{base}_patch_{j:02d}.json"
                    fpath = os.path.join(patches_dir, fname)
                    with open(fpath, "w", encoding="utf-8") as fh:
                        json.dump(data, fh, ensure_ascii=False, indent=2)
                    written.append({"file": fpath, "bytes": os.path.getsize(fpath)})
                    saved += 1
            else:
                data = _jsonify(it)
                base = _slug(it.get("id", idx) if isinstance(it, dict) else idx)
                fname = f"{base}.json"
                fpath = os.path.join(patches_dir, fname)
                with open(fpath, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                written.append({"file": fpath, "bytes": os.path.getsize(fpath)})
                saved += 1
        except Exception as e:
            errors.append({"index": idx, "error": f"{type(e).__name__}: {e}"})
            # keep going

    meta = {
        "count": int(saved),
        "patches": written,
        "debug": {
            "items_len": len(items),
            "run_dir": run_dir,
            "patches_dir": patches_dir,
            "errors": errors,
        },
    }
    return [Artifact(kind="Result", uri="spine://result/patch.apply_files.v1", sha256="", meta=meta)]


# -------------------- capability: main façade --------------------

def run_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Patch engine façade.

    NEW BEHAVIOR:
      - If caller already provides 'items' (verified, parsed patch intents), we
        DO NOT re-invoke llm.engine.run.v1. We directly save patches by calling
        apply_files_v1 and return its result.

    LEGACY BEHAVIOR (fallback):
      - If no 'items' are present, retain old behavior (e.g., dispatch a
        pipeline) to compute them. This prevents breaking older entrypoints.
    """
    p = dict(task.payload or {})
    items = p.get("items") or []
    if not isinstance(items, list):
        items = []

    # Always honor explicit write-ish flags (currently informational)
    write = bool(p.get("write", True))
    dry_run = bool(p.get("dry_run", False))

    # Short-circuit: if we already have items (the usual case from llm.engine.run.v1),
    # save them directly instead of spinning another engine run.
    if items:
        out_base = p.get("out_base") or ""
        sqlalchemy_url = p.get("sqlalchemy_url") or ""
        sqlalchemy_table = p.get("sqlalchemy_table") or ""

        if not out_base:
            return _err("InvalidPayload", "Missing out_base", uri="spine://problem/patch.run.v1")
        if not sqlalchemy_url or not sqlalchemy_table:
            return _err("InvalidPayload", "Missing sqlalchemy_url/sqlalchemy_table", uri="spine://problem/patch.run.v1")

        try:
            # Reuse same task so apply_files_v1 sees the full payload
            return apply_files_v1(task, context)
        except Exception as e:
            import traceback as _tb
            tb = _tb.format_exc()
            return _err("UnhandledException", f"{type(e).__name__}: {e}", {"trace": tb}, uri="spine://problem/patch.run.v1")

    # --- Fallback path (legacy flows that expect patch.run.v1 to run a sub-pipeline) ---
    try:
        from v2.backend.core.spine.bootstrap import Spine
        from v2.backend.core.configuration.loader import get_spine_caps_path

        caps_path = get_spine_caps_path()
        spine = Spine(caps_path=caps_path)
        sub_payload = dict(p.get("engine_payload") or {})

        # Make sure provider/model/db defaults are available to the nested engine
        for k in (
            "provider", "model", "ask_spec",
            "sqlalchemy_url", "sqlalchemy_table",
            "run_fetch_targets", "run_build_prompts", "run_run_llm",
            "run_unpack", "run_sanitize", "run_verify"
        ):
            if k in p and k not in sub_payload:
                sub_payload[k] = p[k]

        # Critically: never allow a nested save/apply to avoid recursion
        sub_payload["run_save_patch"] = False
        sub_payload["run_apply_patch_sandbox"] = False
        sub_payload["run_archive_and_replace"] = False
        sub_payload["run_rollback"] = False

        artifacts = spine.dispatch_capability(
            capability="llm.engine.run.v1",
            payload=sub_payload,
        )
        return artifacts
    except Exception as e:
        import traceback as _tb
        tb = _tb.format_exc()
        return _err("UnhandledException", f"{type(e).__name__}: {e}", {"trace": tb}, uri="spine://problem/patch.run.v1")


__all__ = ["run_v1", "apply_files_v1"]
