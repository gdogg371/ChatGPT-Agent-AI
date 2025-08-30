# v2/backend/core/patch_engine/providers.py
from __future__ import annotations

from typing import Any, Dict, List, Optional
import os, json, re, shutil
from datetime import datetime
import dataclasses
from pathlib import Path

from v2.backend.core.spine.contracts import Artifact, Task

# IO + patch helpers
from v2.backend.core.utils.io.file_ops import FileOps, FileOpsConfig
from v2.backend.core.utils.io.run_dir import RunDirs
from v2.backend.core.utils.io.output_writer import OutputWriter

# Be resilient to either package layout (v2.patches.* or patches.*)
try:
    from v2.patches.core.rewrite import apply_docstring_update
    from v2.patches.core.patch_ops import PatchOps
except Exception:
    from patches.core.rewrite import apply_docstring_update  # type: ignore
    from patches.core.patch_ops import PatchOps  # type: ignore


def _ok(result: Any, *, kind: str = "Result", uri: str = "spine://patch/ok") -> List[Artifact]:
    return [Artifact(kind=kind, uri=uri, sha256="", meta={"result": result})]


def _err(code: str, message: str, details: Dict[str, Any] | None = None, *, uri: str = "spine://patch/error") -> List[Artifact]:
    return [Artifact(kind="Problem", uri=uri, sha256="", meta={"problem": {
        "code": code, "message": message, "retryable": False, "details": dict(details or {})
    }})]


def _slug(x: Any) -> str:
    s = str("" if x is None else x)
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s)
    return s or "item"


def _jsonify(x: Any) -> Any:
    if x is None or isinstance(x, (bool, int, float, str)): return x
    if isinstance(x, dict): return {str(k): _jsonify(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)): return [_jsonify(v) for v in x]
    if dataclasses.is_dataclass(x):
        try: return _jsonify(dataclasses.asdict(x))
        except Exception: pass
    for m in ("model_dump", "dict"):
        if hasattr(x, m) and callable(getattr(x, m)):
            try: return _jsonify(getattr(x, m)())
            except Exception: pass
    if hasattr(x, "_asdict") and callable(getattr(x, "_asdict")):
        try: return _jsonify(x._asdict())
        except Exception: pass
    if hasattr(x, "__dict__"):
        try: return _jsonify(vars(x))
        except Exception: pass
    try: return repr(x)
    except Exception: return ""


# -------------------- normalize & housekeeping helpers --------------------
def _move_all(src: Path, dst: Path) -> None:
    """Move *contents* of src into dst, creating dst if needed."""
    if not src.exists() or not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for p in src.iterdir():
        target = dst / p.name
        try:
            if p.is_dir():
                if target.exists() and target.is_dir():
                    _move_all(p, target)
                    try: p.rmdir()
                    except OSError: pass
                else:
                    shutil.move(str(p), str(target))
            else:
                if target.exists():
                    # Avoid overwriting different files; add suffix
                    stem = target.stem
                    suf = 1
                    while target.exists():
                        target = target.with_name(f"{stem}__{suf}{target.suffix}")
                        suf += 1
                shutil.move(str(p), str(target))
        except Exception:
            # Non-fatal; continue moving others
            continue


def _normalize_verify_dir(run_root: Path) -> None:
    """
    Ensure we have only one 'verify_reports' dir.
    If a stray 'verify reports' (with space) exists, merge its contents into 'verify_reports'
    and remove the stray folder if empty.
    """
    good = run_root / "verify_reports"
    bad = run_root / "verify reports"  # legacy name with space
    if bad.exists() and bad.is_dir():
        _move_all(bad, good)
        # try removing the empty 'bad' dir
        try: bad.rmdir()
        except OSError: pass


def _prune_empty_dirs(run_root: Path, subdirs: List[str]) -> None:
    """
    Remove directories that are empty to avoid clutter in the final package.
    Safe no-op if a directory isn't present or not empty.
    """
    for name in subdirs:
        d = run_root / name
        try:
            if d.exists() and d.is_dir():
                # Check recursively if directory is empty (no files in tree)
                empty = True
                for _, _, files in os.walk(d):
                    if files:
                        empty = False
                        break
                if empty:
                    # remove empty subdirs bottom-up
                    for root, dirs, files in list(os.walk(d, topdown=False)):
                        for sub in dirs:
                            p = Path(root) / sub
                            try: p.rmdir()
                            except OSError: pass
                    try: d.rmdir()
                    except OSError: pass
        except Exception:
            # never fail run for housekeeping
            continue


# -------------------- capability: write patches (UNIFIED DIFFS) + populate run folders --------------------
def apply_files_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Spine capability: patch.apply_files.v1

    Writes:
      - patches/*.patch     (unified diffs)
      - sandbox_applied/... (updated files)
      - items/*.json        (per-item intents)
      - summary.csv         (append-only)
      - raw_prompts/messages.json
      - raw_responses/*.json and all.jsonl
      - batches/batches.jsonl
      - verify_reports/summary.json

    Expected payload fields:
      - out_base: str [required], e.g. "output/patches_received"
      - items: list[dict] (sanitized docstring updates)
      - run_dir | run_id: optional
      - raw_prompts: dict with 'system' and 'user'
      - raw_responses: list[dict] responses from LLM provider
      - prepared_batch: list[dict] prompt build batch
      - verify_summary: dict with 'count' and 'errors'
    """
    p: Dict[str, Any] = dict(getattr(task, "payload", {}) or {})
    out_base: str = str(p.get("out_base") or "").strip()
    raw_items = p.get("items", [])

    if not out_base:
        return _err("InvalidPayload", "Missing out_base", uri="spine://problem/patch.apply_files.v1")

    # normalize items
    if isinstance(raw_items, list):
        items: List[Dict[str, Any]] = [dict(x) for x in raw_items if isinstance(x, dict)]
    elif raw_items is None:
        items = []
    else:
        items = [dict(raw_items) if isinstance(raw_items, dict) else {"value": raw_items}]

    # establish run directory (timestamped if not provided)
    run_dir = str(p.get("run_dir") or "").strip()
    run_id = str(p.get("run_id") or "").strip()
    if not run_dir:
        run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        rd = RunDirs(Path(out_base))
        rd_obj = rd.ensure(run_id)
        run_root: Path = rd_obj.root
    else:
        run_root = Path(run_dir)
        run_root.mkdir(parents=True, exist_ok=True)

    # Writer & helpers
    file_ops = FileOps(FileOpsConfig(preserve_crlf=True))
    out = OutputWriter(run_root)
    patch_ops = PatchOps(file_ops)

    # Ensure a 'patches' directory (legacy package shape)
    patches_root = run_root / "patches"
    patches_root.mkdir(exist_ok=True)

    written: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    # ---------- NEW: persist raw prompts / responses / batch / verify summary ----------
    try:
        rp = p.get("raw_prompts")
        if isinstance(rp, dict) and (rp.get("system") or rp.get("user")):
            (run_root / "raw_prompts").mkdir(exist_ok=True)
            file_ops.write_text(
                run_root / "raw_prompts" / "messages.json",
                json.dumps(_jsonify(rp), ensure_ascii=False, indent=2),
            )
    except Exception as e:
        errors.append({"stage": "raw_prompts", "error": f"{type(e).__name__}: {e}"})

    try:
        rr = p.get("raw_responses")
        if isinstance(rr, list) and rr:
            (run_root / "raw_responses").mkdir(exist_ok=True)
            # per-response JSON
            for i, r in enumerate(rr):
                file_ops.write_text(
                    run_root / "raw_responses" / f"{i:04d}.json",
                    json.dumps(_jsonify(r), ensure_ascii=False, indent=2),
                )
            # aggregate JSONL
            with (run_root / "raw_responses" / "all.jsonl").open("w", encoding="utf-8") as f:
                for r in rr:
                    f.write(json.dumps(_jsonify(r), ensure_ascii=False) + "\n")
    except Exception as e:
        errors.append({"stage": "raw_responses", "error": f"{type(e).__name__}: {e}"})

    try:
        pb = p.get("prepared_batch")
        if isinstance(pb, list) and pb:
            out.append_batch([_jsonify(x) for x in pb])  # batches/batches.jsonl
    except Exception as e:
        errors.append({"stage": "batches", "error": f"{type(e).__name__}: {e}"})

    try:
        vs = p.get("verify_summary")
        if isinstance(vs, dict):
            (run_root / "verify_reports").mkdir(exist_ok=True)
            file_ops.write_text(
                run_root / "verify_reports" / "summary.json",
                json.dumps(_jsonify(vs), ensure_ascii=False, indent=2),
            )
    except Exception as e:
        errors.append({"stage": "verify_reports", "error": f"{type(e).__name__}: {e}"})

    # ---------- patches + sandbox + items + summary ----------
    for idx, it in enumerate(items):
        relpath = (it.get("relpath") or it.get("file") or "").replace("\\", "/")
        abspath = it.get("path") or ""
        doc = it.get("docstring") or ""
        lineno = int(it.get("target_lineno") or 1)

        rec_id = str(it.get("id", idx))
        signature = str(it.get("signature", ""))

        if not relpath or not abspath:
            errors.append({"index": idx, "error": "Missing relpath/path"})
            out.append_summary(rec_id, relpath or "", signature, "", False, "Missing relpath/path")
            continue

        try:
            original_src = file_ops.read_text(Path(abspath))
        except Exception as e:
            errors.append({"index": idx, "error": f"ReadError: {e}", "path": abspath})
            out.append_summary(rec_id, relpath, signature, "", False, f"ReadError: {e}")
            continue

        try:
            updated_src = apply_docstring_update(original_src, lineno, str(doc), relpath=relpath)
        except Exception as e:
            errors.append({"index": idx, "error": f"RewriteError: {e}", "path": abspath})
            out.append_summary(rec_id, relpath, signature, "", False, f"RewriteError: {e}")
            continue

        base = re.sub(r"[^A-Za-z0-9_.-]+", "_", relpath)

        # 1) unified diff into /patches/
        try:
            patch_fp = patch_ops.write_patch(
                run_root=patches_root,
                base_name=base,
                original_src=original_src,
                updated_src=updated_src,
                relpath_label=relpath,
                per_item_suffix=f"__{idx:02d}",
            )
        except Exception as e:
            errors.append({"index": idx, "error": f"PatchWriteError: {e}", "relpath": relpath})
            out.append_summary(rec_id, relpath, signature, "", False, f"PatchWriteError: {e}")
            continue

        # 2) updated file into /sandbox_applied/
        try:
            patch_ops.apply_to_sandbox(run_root, relpath, updated_src)
        except Exception as e:
            errors.append({"index": idx, "warn": f"SandboxWriteWarning: {e}", "relpath": relpath})

        # 3) persist the input item
        try:
            out.write_item({"id": rec_id, **it})
        except Exception:
            pass

        written.append({"patch": str(patch_fp), "relpath": relpath})
        out.append_summary(rec_id, relpath, signature, str(patch_fp), True, "")

    # ---------- FINALIZE: normalize & prune ----------
    try:
        _normalize_verify_dir(run_root)
    except Exception:
        pass

    try:
        _prune_empty_dirs(
            run_root,
            subdirs=[
                "archives",
                "prod_applied",
                "raw_prompts",
                "raw_responses",
                "rollbacks",
                "sandbox_applied",
                "verify reports",   # legacy (will be removed if empty)
                "verify_reports",
                "items",
                "batches",
                # do NOT list 'patches' here—leave even if empty for visibility
            ],
        )
    except Exception:
        pass

    meta = {
        "count": len([w for w in written if w.get("patch")]),
        "run_dir": str(run_root),
        "patches": written,
        "errors": errors,
    }
    return [Artifact(kind="Result", uri="spine://result/patch.apply_files_v1", sha256="", meta=meta)]


def run_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Patch engine façade.

    If 'items' present: write artifacts now (short-circuit).
    Else: legacy fallback (dispatch nested engine with save disabled).
    """
    p = dict(task.payload or {})
    items = p.get("items") or []
    if not isinstance(items, list):
        items = []

    if items:
        out_base = p.get("out_base") or ""
        sqlalchemy_url = p.get("sqlalchemy_url") or ""
        sqlalchemy_table = p.get("sqlalchemy_table") or ""
        if not out_base:
            return _err("InvalidPayload", "Missing out_base", uri="spine://problem/patch.run.v1")
        if not sqlalchemy_url or not sqlalchemy_table:
            return _err("InvalidPayload", "Missing sqlalchemy_url/sqlalchemy_table", uri="spine://problem/patch.run.v1")
        try:
            return apply_files_v1(task, context)
        except Exception as e:
            import traceback as _tb
            tb = _tb.format_exc()
            return _err("UnhandledException", f"{type(e).__name__}: {e}", {"trace": tb}, uri="spine://problem/patch.run.v1")

    # fallback path
    try:
        from v2.backend.core.spine.bootstrap import Spine
        from v2.backend.core.configuration.loader import get_spine_caps_path

        caps_path = get_spine_caps_path()
        spine = Spine(caps_path=caps_path)
        sub_payload = dict(p.get("engine_payload") or {})
        for k in ("provider","model","ask_spec","sqlalchemy_url","sqlalchemy_table",
                  "run_fetch_targets","run_build_prompts","run_run_llm","run_unpack","run_sanitize","run_verify"):
            if k in p and k not in sub_payload:
                sub_payload[k] = p[k]
        sub_payload["run_save_patch"] = False
        sub_payload["run_apply_patch_sandbox"] = False
        sub_payload["run_archive_and_replace"] = False
        sub_payload["run_rollback"] = False

        artifacts = spine.dispatch_capability("llm.engine.run.v1", payload=sub_payload)
        return artifacts
    except Exception as e:
        import traceback as _tb
        tb = _tb.format_exc()
        return _err("UnhandledException", f"{type(e).__name__}: {e}", {"trace": tb}, uri="spine://problem/patch.run.v1")


__all__ = ["run_v1", "apply_files_v1"]



