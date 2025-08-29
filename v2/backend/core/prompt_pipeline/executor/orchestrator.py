# File: v2/backend/core/prompt_pipeline/executor/orchestrator.py
"""
Orchestrator + capability runner for the prompt pipeline.

Exports:
  - capability_run(name, payload, context=None) -> List[Artifact-like]
  - Orchestrator(payload).run()   # legacy wrapper
  - Engine = Orchestrator         # legacy alias

Behavior
--------
1) Prefer Spine registry (if available).
2) Else resolve providers via the executor provider alias map.
3) If a docstring-build capability is missing, use a minimal internal fallback
   so the pipeline can still proceed (for prompts.build.v1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


# --------------------------- Artifact / Task shims ---------------------------

try:
    # Prefer real contracts if available
    from v2.backend.core.spine.contracts import Artifact  # type: ignore
except Exception:  # pragma: no cover
    class Artifact(dict):  # type: ignore
        """Minimal shim that behaves like an Artifact for our purposes."""
        pass


class _TaskShim:
    __slots__ = ("payload", "meta", "data")
    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        self.meta = payload
        self.data = payload

    def __getitem__(self, k: str) -> Any:
        return self.payload[k]

    def get(self, k: str, default: Any = None) -> Any:
        return self.payload.get(k, default)


def _artifact_problem(uri: str, code: str, message: str,
                      *, retryable: bool = False,
                      details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [Artifact(kind="Problem", uri=uri, meta={
        "problem": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details or {},
        }
    })]


def _artifact_ok(uri: str, meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri=uri, meta=meta)]


# ----------------------------- Import utilities ------------------------------

def _import_first(module_name: str, *candidate_attrs: str):
    try:
        mod = __import__(module_name, fromlist=["*"])
    except Exception:
        return None
    for attr in candidate_attrs:
        fn = getattr(mod, attr, None)
        if callable(fn):
            return fn
    return None


def _import_from_any(modules: Sequence[str], candidates: Sequence[str]):
    for mod in modules:
        fn = _import_first(mod, *candidates)
        if callable(fn):
            return fn, mod
    return None, None


# ------------------------------ Fallback builder -----------------------------

def _read_file_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None


def _slice_context(src: str, center_line: int, half_window: int) -> str:
    lines = src.splitlines()
    i0 = max(0, int(center_line) - 1 - half_window)
    i1 = min(len(lines), int(center_line) - 1 + half_window + 1)
    return "\n".join(lines[i0:i1])


def _fallback_build_prompts(payload: Dict[str, Any]) -> List[Artifact]:
    """
    Emergency builder for docstring prompts so the pipeline can proceed even when
    docstring providers are not present. Input is expected to include 'items' or 'records'.
    """
    records = list(payload.get("items") or payload.get("records") or [])
    if not records:
        return _artifact_problem("spine://capability/docstrings.build_prompts.v1",
                                 "NoRecords", "payload.items/records is empty")

    project_root = Path(str(payload.get("project_root") or payload.get("root") or ".")).resolve()
    half = int(payload.get("context_half_window", 25))

    # Try to use your domain builder first (if available)
    system_text = "You are a precise Python documentation assistant."
    try:
        from v2.backend.core.docstrings.prompt_builder import build_system_prompt, build_user_prompt  # type: ignore
        have_pb = True
    except Exception:
        have_pb = False

    prepared: List[Dict[str, Any]] = []
    try:
        from v2.backend.core.docstrings.ast_utils import find_target_by_lineno  # type: ignore
    except Exception:
        find_target_by_lineno = None  # type: ignore

    for rec in records:
        rel = (rec.get("relpath") or rec.get("filepath") or rec.get("file") or "").strip().replace("\\", "/")
        if not rel:
            return _artifact_problem("spine://capability/docstrings.build_prompts.v1",
                                     "BadRecord", f"record missing path fields: {json.dumps(rec)[:200]}")
        abs_path = (project_root / rel).resolve()
        src = _read_file_text(abs_path) or ""
        target_lineno = int(rec.get("target_lineno") or rec.get("lineno") or 1)
        signature = "module"
        has_doc = False
        if find_target_by_lineno and src:
            try:
                info = find_target_by_lineno(src, target_lineno, rel)
                signature = getattr(info, "signature", "module")
                has_doc = bool(getattr(info, "has_docstring", False))
            except Exception:
                signature = "module"
                has_doc = False

        prepared.append({
            "id": str(rec.get("id") or f"{rel}#{target_lineno}"),
            "relpath": rel,
            "path": str(abs_path),
            "signature": signature,
            "target_lineno": target_lineno,
            "has_docstring": has_doc,
            "description": (rec.get("description") or "").strip(),
            "context_code": _slice_context(src, target_lineno, half) if src else "",
            "symbol_type": rec.get("symbol_type") or "unknown",
        })

    if have_pb:
        system_text = build_system_prompt()
        # Adapt payload for user prompt
        batch_for_prompt = [{
            "id": it["id"],
            "mode": "rewrite" if it["has_docstring"] else "create",
            "signature": it["signature"],
            "has_docstring": it["has_docstring"],
            "description": it.get("description", ""),
            "context_code": it.get("context_code", ""),
        } for it in prepared]
        user_text = build_user_prompt(batch_for_prompt)
    else:
        # Minimal text prompt
        lines: List[str] = [
            "For each item below, return a JSON list like:\n"
            '[{\"id\": \"\", \"docstring\": \"\"}]',
            "Constraints:",
            " - 'docstring' MUST be non-empty and meaningful (~20+ words unless trivial).",
            " - Do NOT include triple quotes; return just the string content.",
            " - For modules: describe what the module provides and how it fits.",
            "Items:",
        ]
        for it in prepared:
            lines.append("--")
            lines.append(f"id: {it['id']}")
            lines.append(f"mode: {'rewrite' if it['has_docstring'] else 'create'}")
            lines.append(f"signature: {it['signature']}")
            desc = (it.get("description") or "").strip()
            if desc:
                lines.append(f"description: {desc}")
            ctx = it.get("context_code") or ""
            if ctx:
                lines.append("context_code:\n```python")
                lines.append(ctx)
                lines.append("```")
        lines.append("\nReturn ONLY the JSON list as the entire response.")
        user_text = "\n".join(lines)

    result = {
        "messages": {"system": system_text, "user": user_text},
        "batch": prepared,
        "ids": [it["id"] for it in prepared],
    }
    return _artifact_ok("spine://docstrings/prompts.build.v1", {"result": result})


# ------------------------------ capability_run -------------------------------

def capability_run(name: str, payload: Any, context: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    """
    Execute a pipeline capability by name.

    Order:
      1) Try Spine registry: v2.backend.core.spine.registry.run(name, payload, context)
      2) Else import a provider using the executor provider alias map.
      3) If the provider is missing and the capability is a docstring build, run a
         minimal internal fallback so the pipeline can proceed.
    """
    ctx: Dict[str, Any] = context or {}

    # 1) Prefer registry if present
    try:
        from v2.backend.core.spine.registry import run as spine_run  # type: ignore
        res = spine_run(name, payload, ctx)
        if isinstance(res, list) and res:
            return res  # Assume registry returns Artifact-like list
    except Exception:
        pass

    # 2) Fallback to provider alias map
    try:
        from .providers import get_capability_map  # type: ignore
        cap_map = get_capability_map()
    except Exception:
        cap_map = {}

    mapping = cap_map.get(name)
    if not mapping:
        # Special case: allow prompts.build.v1 to fall back to the internal builder
        if name in ("prompts.build.v1", "docstrings.build_prompts.v1"):
            p = payload if isinstance(payload, dict) else getattr(payload, "payload", {}) or {}
            return _fallback_build_prompts(p)
        return _artifact_problem(f"spine://capability/{name}", "CapabilityMissing",
                                 f"No mapping for capability '{name}'", retryable=False)

    modules, candidates = mapping
    fn, _mod = _import_from_any(modules, candidates)

    # Built-in fallback for docstring build when real provider is missing
    if not callable(fn) and name in ("prompts.build.v1", "docstrings.build_prompts.v1"):
        p = payload if isinstance(payload, dict) else getattr(payload, "payload", {}) or {}
        return _fallback_build_prompts(p)

    if not callable(fn):
        return _artifact_problem(f"spine://capability/{name}", "ProviderMissing",
                                 f"Provider not found for '{name}'",
                                 details={"modules": modules, "candidates": candidates})

    task = payload if hasattr(payload, "payload") else _TaskShim(payload if isinstance(payload, dict) else {"payload": payload})
    try:
        try:
            return fn(task, ctx)  # type: ignore[misc]
        except TypeError:
            return fn(task)  # type: ignore[misc]
    except Exception as e:
        return _artifact_problem(f"spine://capability/{name}", "ProviderError",
                                 f"{type(e).__name__}: {e}", retryable=False, details={})


# --------------------------- Legacy Orchestrator -----------------------------

@dataclass
class Orchestrator:
    """Compatibility wrapper that calls the engine capability via Spine (if present),
    falling back to the local alias/capability runner."""
    payload: Dict[str, Any]

    def run(self) -> List[Artifact]:
        # Try Spine engine first
        try:
            from v2.backend.core.spine import Spine  # type: ignore
            from v2.backend.core.configuration.spine_paths import SPINE_CAPS_PATH  # type: ignore
            spine = Spine(caps_path=SPINE_CAPS_PATH)
            return spine.dispatch_capability(
                capability="llm.engine.run.v1",
                payload=self.payload,
                intent="pipeline",
                subject=self.payload.get("project_root") or "-",
                context={"shim": "executor.orchestrator"},
            )
        except Exception:
            # Fallback: call engine.run via local map
            return capability_run("llm.engine.run.v1", self.payload, {"shim": "executor.orchestrator"})


# Legacy alias
Engine = Orchestrator

__all__ = ["capability_run", "Orchestrator", "Engine"]




