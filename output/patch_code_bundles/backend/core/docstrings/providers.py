# v2/backend/core/docstrings/providers.py
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from v2.backend.core.spine.contracts import Artifact
from v2.backend.core.docstrings import prompt_builder
from v2.backend.core.docstrings.sanitize import sanitize_docstring
from v2.backend.core.docstrings.verify import DocstringVerifier
from v2.backend.core.docstrings.ast_utils import find_target_by_lineno
from v2.backend.core.introspect.read_docstrings import DocStringAnalyzer


# ---------- helpers ----------

def _ok(result: Any, *, kind: str = "Result") -> List[Artifact]:
    return [
        Artifact(
            kind=kind,
            uri="spine://docstrings/ok",
            sha256="",
            meta={"result": result},
        )
    ]


def _err(code: str, message: str, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [
        Artifact(
            kind="Problem",
            uri="spine://docstrings/error",
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


# ---------- capabilities ----------

def scan_python_v1(payload: Dict[str, Any]) -> List[Artifact]:
    """
    capability: docstrings.scan.python.v1

    Input payload (all optional):
      - root: str                   # folder to scan (env DOCSTRING_ROOT used if absent)
      - writer_mode: str            # 'introspection_index' or 'agent_insights'
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


def build_prompts_v1(payload: Dict[str, Any]) -> List[Artifact]:
    """
    capability: docstrings.prompts.build.v1

    Input payload:
      - records: List[dict]     # rows with at least: id, filepath, lineno, symbol_type, name
      - project_root: str       # absolute repo root
      - context_half_window: int=25
      - description_field: str  # optional; name of field in record with human description

    Output (meta.result):
      {
        "messages": {"system": str, "user": str},
        "batch": [ {id, relpath, path, signature, target_lineno, has_docstring, description, context_code} ],
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

    # Build messages
    system_text = prompt_builder.build_system_prompt()
    # Adapter expects id/mode/signature/has_existing/description/context
    batch_for_prompt: List[Dict[str, str]] = []
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


def sanitize_outputs_v1(payload: Dict[str, Any]) -> List[Artifact]:
    """
    capability: docstrings.sanitize.v1

    Input payload:
      - results: List[dict]  # items from LLM: {"id","mode","docstring"}
      - items:   List[dict]  # original prepared items (from build_prompts_v1) keyed by id

    Output:
      meta.result = {"sanitized": [ {"id","docstring","signature","relpath","path","target_lineno"} ]}
    """
    results = list(payload.get("results") or [])
    items = {str(it.get("id")): it for it in (payload.get("items") or [])}
    if not results:
        return _err("NoResults", "payload.results is empty")

    sanitized: List[Dict[str, Any]] = []
    for r in results:
        rid = str(r.get("id"))
        doc_raw = r.get("docstring") or ""
        it = items.get(rid, {})
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
    return _ok({"sanitized": sanitized})


def verify_batch_v1(payload: Dict[str, Any]) -> List[Artifact]:
    """
    capability: docstrings.verify.v1

    Input:
      - sanitized: List[{"id","docstring","signature", ...}]

    Output:
      meta.result = {"reports": [{"id","ok", "issues": [..]}]}
    """
    verifier = DocstringVerifier()
    items = list(payload.get("sanitized") or [])
    if not items:
        return _err("NoItems", "payload.sanitized is empty")

    reports: List[Dict[str, Any]] = []
    for it in items:
        doc = str(it.get("docstring") or "")
        sig = str(it.get("signature") or "module")
        ok1, i1 = verifier.pep257_minimal(doc)
        ok2, i2 = verifier.params_consistency(doc, sig)
        issues = list(i1) + list(i2)
        reports.append({"id": str(it.get("id")), "ok": (ok1 and ok2 and not issues), "issues": issues})
    return _ok({"reports": reports})


# Optional: simple JSON echo to help debug pipelines that expect JSON-serializable output
@dataclass
class EchoJson:
    payload: Dict[str, Any]

    def to_artifact(self) -> List[Artifact]:
        return _ok(json.loads(json.dumps(self.payload)))
