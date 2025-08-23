# File: v2/backend/core/docstrings/providers.py
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from v2.backend.core.spine.contracts import Artifact, Task
from v2.backend.core.docstrings import prompt_builder
from v2.backend.core.docstrings.sanitize import sanitize_docstring
from v2.backend.core.docstrings.verify import DocstringVerifier
from v2.backend.core.docstrings.ast_utils import find_target_by_lineno
from v2.backend.core.introspect.read_docstrings import DocStringAnalyzer


# ---------- helpers ----------


def _ok(result: Any, *, kind: str = "Result", uri: str = "spine://docstrings/ok") -> List[Artifact]:
    return [
        Artifact(
            kind=kind,
            uri=uri,
            sha256="",
            meta={"result": result},
        )
    ]


def _err(code: str, message: str, details: Optional[Dict[str, Any]] = None, *, uri: str = "spine://docstrings/error") -> List[Artifact]:
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


def _read_file_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None


def _slice_context(src: str, center_line: int, half_window: int) -> str:
    lines = src.splitlines()
    # convert to 0-based indices
    i0 = max(0, center_line - 1 - half_window)
    i1 = min(len(lines), center_line - 1 + half_window + 1)
    return "\n".join(lines[i0:i1])


def _as_list_from_dict_or_list(x: Any) -> List[Dict[str, Any]]:
    """Accept list[dict] or dict[str,dict] → list[dict]."""
    if isinstance(x, dict):
        return [dict(v, id=str(k)) if "id" not in v else v for k, v in x.items() if isinstance(v, dict)]
    if isinstance(x, list):
        return [dict(it) for it in x if isinstance(it, dict)]
    return []


def _prepared_items_from_context(context: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Try to retrieve the prepared items produced by the generic prompts builder:
      context['state']['build']['result']['items']  (list of dicts)
    Return a dict by id.
    """
    try:
        items = context.get("state", {}).get("build", {}).get("result", {}).get("items") or []
        return {str(it.get("id")): it for it in items if isinstance(it, dict)}
    except Exception:
        return {}


# ---------- capability implementations (payload-level) ----------


def _scan_python_v1(payload: Dict[str, Any]) -> List[Artifact]:
    """
    capability: docstrings.scan.python.v1

    Input payload (all optional):
      - root: str  # folder to scan (env DOCSTRING_ROOT used if absent)
      - writer_mode: str  # 'introspection_index' or 'agent_insights'
      - agent_id: int

    Side effects: writes rows via DocstringWriter (matches existing behavior).
    Returns a single Artifact with meta.result = {"stats": {...}}.
    """
    try:
        analyzer = DocStringAnalyzer()
        if payload.get("root"):
            # override at runtime
            analyzer.ROOT_DIR = str(payload["root"])
            analyzer.root_path = Path(str(payload["root"])).resolve()
        if payload.get("writer_mode"):
            analyzer.WRITER_MODE = str(payload["writer_mode"])
        if payload.get("agent_id") is not None:
            analyzer.AGENT_ID = int(payload["agent_id"])

        stats = analyzer.traverse_and_write()
        return _ok({"stats": stats})
    except FileNotFoundError as e:
        return _err("ScanRootNotFound", str(e))
    except Exception as e:
        return _err("ScanFailed", f"{e!r}")


def _build_prompts_v1(payload: Dict[str, Any]) -> List[Artifact]:
    """
    capability: docstrings.prompts.build.v1

    Input payload:
      - records: List[dict]  # rows with at least: id, filepath, lineno, symbol_type, name
      - project_root: str    # absolute repo root
      - context_half_window: int=25
      - description_field: str  # optional; name of field in record with human description

    Output (meta.result):
      {
        "messages": {"system": str, "user": str},
        "batch": [
          {id, relpath, path, signature, target_lineno, has_docstring, description, context_code}
        ],
        "ids": [id, ...]
      }
    """
    records = list(payload.get("records") or [])
    if not records:
        return _err("NoRecords", "payload.records is empty")

    project_root = Path(str(payload.get("project_root") or ".")).resolve()
    half = int(payload.get("context_half_window", 25))
    desc_key = str(payload.get("description_field") or "description")

    prepared: List[Dict[str, Any]] = []
    for rec in records:
        rel = (rec.get("filepath") or rec.get("file") or "").strip().replace("\\", "/")
        if not rel:
            return _err("BadRecord", f"record missing 'filepath': {rec!r}")
        abs_path = (project_root / rel).resolve()
        src = _read_file_text(abs_path)
        if src is None:
            # still emit a stub so downstream can see the failure context
            prepared.append(
                {
                    "id": str(rec.get("id") or f"{rel}#{rec.get('lineno', 1)}"),
                    "relpath": rel,
                    "path": str(abs_path),
                    "signature": "module",
                    "target_lineno": int(rec.get("lineno", 1) or 1),
                    "has_docstring": False,
                    "description": rec.get(desc_key) or "",
                    "context_code": "",
                    "symbol_type": rec.get("symbol_type") or "unknown",
                }
            )
            continue

        target_lineno = int(rec.get("lineno", 1) or 1)
        try:
            info = find_target_by_lineno(src, target_lineno, rel)
            signature = info.signature
            has_doc = bool(info.has_docstring)
        except Exception:
            signature = "module"
            has_doc = False

        prepared.append(
            {
                "id": str(rec.get("id") or f"{rel}#{target_lineno}"),
                "relpath": rel,
                "path": str(abs_path),
                "signature": signature,
                "target_lineno": target_lineno,
                "has_docstring": has_doc,
                "description": rec.get(desc_key) or "",
                "context_code": _slice_context(src, target_lineno, half),
                "symbol_type": rec.get("symbol_type") or "unknown",
            }
        )

    # Build messages: Adapter expects id/mode/signature/has_docstring/description/context
    system_text = prompt_builder.build_system_prompt()

    batch_for_prompt: List[Dict[str, Any]] = []
    for it in prepared:
        batch_for_prompt.append(
            {
                "id": it["id"],
                "mode": "rewrite" if it["has_docstring"] else "create",
                "signature": it["signature"],
                "has_docstring": it["has_docstring"],
                "description": it.get("description", ""),
                "context_code": it.get("context_code", ""),
            }
        )
    user_text = prompt_builder.build_user_prompt(batch_for_prompt)

    result = {
        "messages": {"system": system_text, "user": user_text},
        "batch": prepared,
        "ids": [it["id"] for it in prepared],
    }
    return _ok(result)


def _sanitize_outputs_v1(payload: Dict[str, Any], context: Dict[str, Any]) -> List[Artifact]:
    """
    capability: docstrings.sanitize.v1

    Accepts multiple shapes for compatibility with different pipelines:

      Variant A (legacy):
        - results: List[{"id","mode","docstring"}]
        - items:   List[prepared items]  (to provide signature/paths)

      Variant B (current generic pipeline):
        - items: Dict[id -> {"docstring", ...}]  (from results.unpack.v1 baton.parsed)
        - (prepared items are looked up from pipeline context 'build.result.items')

    Output:
      meta.result = List[{"id","docstring","signature","relpath","path","target_lineno"}]
    """
    # Gather results to sanitize
    results = payload.get("results")
    if not results:
        # Accept dict mapping id->object or list under "items"
        results = payload.get("items")
    results_list = _as_list_from_dict_or_list(results)

    if not results_list:
        return _err("NoResults", "no results to sanitize", {"keys": list(payload.keys())})

    # Prepared items: explicit list/dict in payload takes precedence
    prepared_map: Dict[str, Dict[str, Any]] = {}
    explicit_prepared = payload.get("prepared_items") or payload.get("items_prepared")
    if explicit_prepared:
        if isinstance(explicit_prepared, dict):
            prepared_map = {str(k): v for k, v in explicit_prepared.items() if isinstance(v, dict)}
        elif isinstance(explicit_prepared, list):
            prepared_map = {str(it.get("id")): it for it in explicit_prepared if isinstance(it, dict)}
    else:
        # Fallback to pipeline context from the 'build' step
        prepared_map = _prepared_items_from_context(context or {})

    sanitized: List[Dict[str, Any]] = []
    for r in results_list:
        rid = str(r.get("id"))
        doc_raw = r.get("docstring") or ""
        it = prepared_map.get(rid, {})
        sig = it.get("signature")
        sym_kind = it.get("symbol_type")
        clean = sanitize_docstring(str(doc_raw), signature=sig, symbol_kind=sym_kind)
        sanitized.append(
            {
                "id": rid,
                "docstring": clean,
                "signature": sig,
                "relpath": it.get("relpath"),
                "path": it.get("path"),
                "target_lineno": int(it.get("target_lineno") or 1),
            }
        )

    return _ok(sanitized)


def _verify_batch_v1(payload: Dict[str, Any]) -> List[Artifact]:
    """
    capability: docstrings.verify.v1

    Input (accepts either key for compatibility):
      - sanitized: List[{"id","docstring","signature", ...}]
      - items:     List[...same as 'sanitized'...]

    Output:
      meta.result = {"reports": [{"id","ok", "issues": [..]}]}
    """
    verifier = DocstringVerifier()
    items_any = payload.get("sanitized") or payload.get("items") or []
    # Accept dict or list
    items = _as_list_from_dict_or_list(items_any)
    if not items:
        return _err("NoItems", "no items to verify")

    reports: List[Dict[str, Any]] = []
    for it in items:
        doc = str(it.get("docstring") or "")
        sig = str(it.get("signature") or "module")
        ok1, i1 = verifier.pep257_minimal(doc)
        ok2, i2 = verifier.params_consistency(doc, sig)
        issues = list(i1) + list(i2)
        reports.append({"id": str(it.get("id")), "ok": (ok1 and ok2 and not issues), "issues": issues})

    return _ok({"reports": reports}, uri="spine://docstrings/verify")


# ---------- capability entrypoints (Registry expects (task, context)) ----------


def scan_python_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    return _scan_python_v1(dict(task.payload or {}))


def build_prompts_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    return _build_prompts_v1(dict(task.payload or {}))


def sanitize_outputs_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    return _sanitize_outputs_v1(dict(task.payload or {}), dict(context or {}))


def verify_batch_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    return _verify_batch_v1(dict(task.payload or {}))


# Optional: simple JSON echo to help debug pipelines that expect JSON-serializable output
@dataclass
class EchoJson:
    payload: Dict[str, Any]

    def to_artifact(self) -> List[Artifact]:
        return _ok(json.loads(json.dumps(self.payload)))


if __name__ == "__main__":
    # Minimal static self-tests for quick sanity checking without DB/LLM.
    failures = 0

    # 1) Sanitize accepts dict-form results and picks up prepared items from context
    dummy_context = {
        "state": {
            "build": {
                "result": {
                    "items": [
                        {"id": "A", "signature": "def foo(x, y): ...", "relpath": "m.py", "path": "/tmp/m.py", "target_lineno": 1},
                        {"id": "B", "signature": "class Bar: ...", "relpath": "m.py", "path": "/tmp/m.py", "target_lineno": 10},
                    ]
                }
            }
        }
    }
    payload_san = {"items": {"A": {"docstring": "Do A."}, "B": {"docstring": "Do B."}}}
    art1 = _sanitize_outputs_v1(payload_san, dummy_context)
    ok1 = bool(art1 and art1[0].kind == "Result" and isinstance(art1[0].meta.get("result"), list) and len(art1[0].meta["result"]) == 2)
    print("[docstrings.providers.selftest] sanitize:", "OK" if ok1 else "FAIL")
    failures += 0 if ok1 else 1

    # 2) Verify accepts 'items' list
    items = art1[0].meta["result"]
    art2 = _verify_batch_v1({"items": items})
    ok2 = bool(art2 and art2[0].kind == "Result" and "reports" in (art2[0].meta.get("result") or {}))
    print("[docstrings.providers.selftest] verify:", "OK" if ok2 else "FAIL")
    failures += 0 if ok2 else 1

    raise SystemExit(failures)

