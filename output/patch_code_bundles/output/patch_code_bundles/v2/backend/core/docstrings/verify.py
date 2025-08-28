# v2/backend/core/docstrings/verify.py
from __future__ import annotations
import os
from typing import Any, Dict, List, Optional

try:
    from v2.backend.core.spine.registry import register  # type: ignore
    register("verify.v1", "v2.backend.core.docstrings.verify:verify_batch_v1")
except Exception:
    pass



# --- Artifact shim ---
try:
    from v2.backend.core.spine.contracts import Artifact  # type: ignore
except Exception:  # pragma: no cover
    class Artifact:  # type: ignore
        def __init__(self, kind: str, uri: str, sha256: str = "", meta: Dict[str, Any] | None = None):
            self.kind = kind
            self.uri = uri
            self.sha256 = sha256
            self.meta = meta or {}

def _ok(uri: str, meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri=uri, sha256="", meta=meta)]

def _problem(uri: str, code: str, msg: str, *, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [Artifact(kind="Problem", uri=uri, sha256="", meta={
        "problem": {"code": code, "message": msg, "retryable": False, "details": details or {}}
    })]

def _payload(task_like: Any) -> Dict[str, Any]:
    if isinstance(task_like, dict):
        return task_like
    for a in ("payload", "meta", "data"):
        v = getattr(task_like, a, None)
        if isinstance(v, dict):
            return v
    return {}

def verify_batch_v1(task: Any, context: Dict[str, Any] | None = None) -> List[Artifact]:
    """
    Capability: docstrings.verify.v1

    Input:
      {
        "items": [
          {"id": str, "docstring": str, "relpath"|"filepath": str,
           "target_lineno": int, "signature": str, "has_docstring": bool}
        ],
        "policy": "lenient" | "strict"  # default: lenient
      }

    Output:
      [Artifact(kind="Result", meta={"result": {"items": [...], "reports": [...]}})]
    """
    p = _payload(task)
    items = list(p.get("items") or [])
    if not isinstance(items, list):
        return _problem("spine://docstrings/verify_v1", "InvalidPayload", "'items' must be a list")

    policy = str(p.get("policy") or os.environ.get("DOCSTRING_VERIFY_POLICY", "lenient")).lower().strip()
    try:
        width = max(1, int(os.environ.get("DOCSTRING_WIDTH", "72")))
    except Exception:
        width = 72

    reports: List[Dict[str, Any]] = []
    ok_items: List[Dict[str, Any]] = []

    for it in items:
        id_ = str(it.get("id") or "")
        ds = str(it.get("docstring") or "")
        rel = str(it.get("relpath") or it.get("filepath") or "")

        errs: List[str] = []
        warns: List[str] = []

        if not id_:
            errs.append("missing id")
        if not rel:
            errs.append("missing relpath/filepath")
        if not ds.strip():
            errs.append("empty docstring")
        if '"""' in ds or "'''" in ds:
            errs.append("docstring contains triple quotes")

        if not (ds.startswith("\n") and ds.endswith("\n")):
            (errs if policy == "strict" else warns).append(
                "docstring must start and end with a newline" if policy == "strict"
                else "docstring should start and end with a newline"
            )

        if any(line.endswith((" ", "\t")) for line in ds.splitlines()):
            (errs if policy == "strict" else warns).append("trailing whitespace detected")

        over = [len(line) for line in ds.splitlines() if len(line) > width]
        if over:
            (errs if policy == "strict" else warns).append(
                f"{'lines exceed' if policy=='strict' else str(len(over))+' line(s) exceed'} width {width}"
            )

        summary = next((ln.strip() for ln in ds.splitlines() if ln.strip()), "")
        if not summary:
            errs.append("missing summary line")
        else:
            if len(summary.split()) < 3:
                warns.append("summary line is very short")
            if summary[-1:] not in ".!?":
                warns.append("summary should end with punctuation")

        if policy == "strict":
            allow = len(errs) == 0
        else:
            critical = {"empty docstring", "docstring contains triple quotes", "missing id", "missing relpath/filepath"}
            allow = not any(e in critical for e in errs)

        reports.append({"id": id_, "errors": errs, "warnings": warns})
        if allow:
            ok_items.append(it)

    return _ok("spine://docstrings/verify_v1", {"result": {"items": ok_items, "reports": reports}})

__all__ = ["verify_batch_v1"]

