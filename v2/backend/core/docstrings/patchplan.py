from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


try:
    from v2.backend.core.spine.registry import register  # type: ignore
    register("patch.plan.v1", "v2.backend.core.docstrings.patchplan:build_patchplan_v1")
except Exception:
    pass


# Artifact shim (safe if spine artifacts aren't importable yet)
try:
    from v2.backend.core.spine.contracts import Artifact  # type: ignore
except Exception:
    class Artifact:  # type: ignore
        def __init__(self, kind: str, uri: str, sha256: str = "", meta: Dict[str, Any] | None = None):
            self.kind = kind
            self.uri = uri
            self.sha256 = sha256
            self.meta = meta or {}

from v2.backend.core.patch_engine.plan import PatchPlan, PatchOp, ReplaceRange, InsertAt
from v2.backend.core.docstrings.formatter import format_inner_docstring, render_docstring_block
from v2.backend.core.docstrings.locator import (
    find_module_docstring_span,
    find_symbol_docstring_span,
    find_orphan_module_string_span,
)


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


def build_patchplan_v1(task: Any, context: Dict[str, Any] | None = None) -> List[Artifact]:
    """
    Capability: docstrings.patchplan.v1

    Input payload:
    {
      "items": [
         { "id": str, "docstring": str, "relpath": str, "path": str,
           "target_lineno": int, "signature": "module"|"function"|"class" }
      ],
      "project_root": "..."
      "width": 72
    }

    Output meta:
    {
      "plan": { "ops": [...] }  # serializable dataclasses
    }
    """
    p = _payload(task)
    items = list(p.get("items") or [])
    if not isinstance(items, list):
        return _problem("spine://docstrings/patchplan_v1", "InvalidPayload", "'items' must be a list")

    project_root = Path(p.get("project_root") or ".").resolve()
    width = int(p.get("width") or 72)

    ops: List[PatchOp] = []

    for it in items:
        rel = str(it.get("relpath") or "").strip().replace("\\", "/")
        abs_path = str(it.get("path") or "").strip()
        if not abs_path:
            abs_path = str((project_root / rel).resolve())
        path = Path(abs_path)

        if not path.exists():
            # If the file doesn't exist yet, insert at file_start
            inner = format_inner_docstring(str(it.get("docstring") or ""), width=width)
            block = "".join(render_docstring_block(inner, indent=""))
            ops.append(InsertAt(relpath=rel, anchor="file_start", line=None, new_text=block))
            continue

        src = path.read_text(encoding="utf-8", errors="replace")

        signature = (it.get("signature") or "module").lower()
        lineno = int(it.get("target_lineno") or 1)

        # Format the new docstring block
        inner = format_inner_docstring(str(it.get("docstring") or ""), width=width)

        # Determine indentation: module docstrings sit at column 0, for defs/classes we align to body level
        indent = ""
        if signature in ("function", "class", "asyncfunction", "async function"):
            # Heuristic: find first non-empty, non-comment line after the header to infer indent
            lines = src.splitlines(keepends=True)
            header_idx = max(lineno - 1, 0)
            indent = ""
            for i in range(header_idx + 1, len(lines)):
                s = lines[i]
                if s.strip() == "" or s.lstrip().startswith("#"):
                    continue
                indent = s[: len(s) - len(s.lstrip(" \t"))]
                break

        block_lines = render_docstring_block(inner, indent=indent)
        new_text = "".join(block_lines)

        if signature == "module":
            # Prefer true AST module docstring
            span = find_module_docstring_span(src)
            if span:
                ops.append(ReplaceRange(relpath=rel, start_line=span[0] + 1, end_line=span[1] + 1, new_text=new_text))
            else:
                # Replace a near-top orphan block if present, else insert after shebang/encoding
                orphan = find_orphan_module_string_span(src)
                if orphan:
                    ops.append(ReplaceRange(relpath=rel, start_line=orphan[0] + 1, end_line=orphan[1] + 1, new_text=new_text))
                else:
                    ops.append(InsertAt(relpath=rel, anchor="after_shebang_and_encoding", line=None, new_text=new_text))
        else:
            # function/class target
            span = find_symbol_docstring_span(src, target_lineno=lineno)
            if span:
                ops.append(ReplaceRange(relpath=rel, start_line=span[0] + 1, end_line=span[1] + 1, new_text=new_text))
            else:
                # Insert right after the header line
                ops.append(InsertAt(relpath=rel, anchor="after_line", line=lineno, new_text=new_text))

    plan = PatchPlan(ops=ops)

    # Return as plain dicts for easy serialization through the spine artifact
    serializable_ops: List[Dict[str, Any]] = []
    for op in plan.ops:
        d = asdict(op)
        d["op"] = type(op).__name__
        serializable_ops.append(d)

    # Register this domain provider under a generic capability name.
    try:
        from v2.backend.core.spine.registry import register as spine_register  # type: ignore
        spine_register("patch.plan.v1", "v2.backend.core.docstrings.patchplan:build_patchplan_v1")
    except Exception:
        pass

    return _ok("spine://docstrings/patchplan_v1", {"plan": {"ops": serializable_ops}})
