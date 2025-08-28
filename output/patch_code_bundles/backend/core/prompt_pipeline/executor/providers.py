# v2/backend/core/prompt_pipeline/executor/providers.py
from __future__ import annotations
import os
from typing import Any, Dict, List, Optional, Tuple, Sequence
from pathlib import Path

# Artifact shim (in case spine.contracts isn’t importable at import time)
try:
    from v2.backend.core.spine.contracts import Artifact  # type: ignore
except Exception:
    class Artifact:  # type: ignore
        def __init__(self, kind: str, uri: str, sha256: str = "", meta: Dict[str, Any] | None = None):
            self.kind = kind
            self.uri = uri
            self.sha256 = sha256
            self.meta = meta or {}

def _artifact_problem(uri: str, code: str, message: str, *, retryable: bool = False,
                      details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [Artifact(kind="Problem", uri=uri, sha256="", meta={
        "problem": {"code": code, "message": message, "retryable": retryable, "details": details or {}}
    })]

def _artifact_ok(uri: str, meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri=uri, sha256="", meta=meta)]

# Task shim so providers that expect task.payload keep working
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

# ---- 1) Prefer the spine registry if available --------------------------------
def _registry_run(name: str, payload: Any, context: Dict[str, Any]) -> Optional[List[Artifact]]:
    try:
        from v2.backend.core.spine.registry import run as spine_run  # type: ignore
        res = spine_run(name, payload, context)
        if isinstance(res, list) and res and isinstance(res[0], Artifact):
            return res
        return None
    except Exception:
        return None

# ---- 2) Import helpers --------------------------------------------------------
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

# ---- 3) Minimal utilities used by the fallback builder ------------------------
def _read_file_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

def _slice_context(src: str, center_line: int, half_window: int) -> str:
    # center_line is 1-based; we slice safely
    lines = src.splitlines()
    i0 = max(0, int(center_line) - 1 - half_window)
    i1 = min(len(lines), int(center_line) - 1 + half_window + 1)
    return "\n".join(lines[i0:i1])

def _fallback_build_prompts(payload: Dict[str, Any]) -> List[Artifact]:
    """
    Internal emergency builder for capability: docstrings.build_prompts.v1

    Input payload:
      - records: List[dict] with keys like id, filepath (or file), lineno, symbol_type, name, description?
      - project_root: str
      - context_half_window: int (default 25)
      - description_field: str (default "description")

    Output meta:
      {
        "result": {
          "messages": {"system": str, "user": str},
          "batch": [ {id, relpath, path, signature, target_lineno, has_docstring, description, context_code, symbol_type} ],
          "ids": [ ... ]
        }
      }
    """
    records = list(payload.get("records") or [])
    if not records:
        return _artifact_problem("spine://capability/docstrings.build_prompts.v1",
                                 "NoRecords", "payload.records is empty")

    project_root = Path(str(payload.get("project_root") or ".")).resolve()
    half = int(payload.get("context_half_window", 25))
    desc_key = str(payload.get("description_field") or "description")

    # Try to use your prompt builder functions if present
    system_text = "You are a precise Python documentation assistant."
    user_text_header = (
        "For each item below, return a JSON list like:\n"
        '[{"id": "", "docstring": ""}]\n'
        "Constraints:\n"
        " - 'docstring' MUST be non-empty and meaningful (≈20+ words unless trivial).\n"
        " - Do NOT include triple quotes; return just the string content.\n"
        " - For modules: describe what the module provides and how it fits.\n"
        "Items:\n"
    )
    try:
        from v2.backend.core.docstrings.prompt_builder import build_system_prompt, build_user_prompt  # type: ignore
        have_pb = True
    except Exception:
        have_pb = False

    prepared: List[Dict[str, Any]] = []
    # Optional signature detection if available
    try:
        from v2.backend.core.docstrings.ast_utils import find_target_by_lineno  # type: ignore
    except Exception:
        find_target_by_lineno = None  # type: ignore

    for rec in records:
        rel = (rec.get("filepath") or rec.get("file") or "").strip().replace("\\", "/")
        if not rel:
            return _artifact_problem("spine://capability/docstrings.build_prompts.v1",
                                     "BadRecord", f"record missing 'filepath': {rec!r}")
        abs_path = (project_root / rel).resolve()
        src = _read_file_text(abs_path) or ""
        target_lineno = int(rec.get("lineno", 1) or 1)

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
            "description": rec.get(desc_key) or "",
            "context_code": _slice_context(src, target_lineno, half) if src else "",
            "symbol_type": rec.get("symbol_type") or "unknown",
        })

    # Messages
    if have_pb:
        system_text = build_system_prompt()
        # Build batch for the prompt adapter (mode/signature/has_docstring/description/context)
        batch_for_prompt: List[Dict[str, Any]] = []
        for it in prepared:
            batch_for_prompt.append({
                "id": it["id"],
                "mode": "rewrite" if it["has_docstring"] else "create",
                "signature": it["signature"],
                "has_docstring": it["has_docstring"],
                "description": it.get("description", ""),
                "context_code": it.get("context_code", ""),
            })
        user_text = build_user_prompt(batch_for_prompt)
    else:
        # Minimal “always works” fallback prompt
        lines: List[str] = [user_text_header]
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

# ---- 4) Capability map --------------------------------------------------------
# Map capability → (modules, candidate function names)
_CAP_MAP: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    # LLM
    "llm.complete_batches.v1": (
        ("v2.backend.core.prompt_pipeline.llm.providers",),
        ("complete_batches_v1", "complete_v1"),
    ),
    # Introspect / fetch
    "introspect.fetch.v1": (
        ("v2.backend.core.introspect.providers",),
        ("fetch_v1", "fetch"),
    ),
    # BUILD: try multiple plausible modules; if none found, we’ll use the fallback
    "docstrings.build_prompts.v1": (
        (
            "v2.backend.core.docstrings.providers",
            "v2.backend.core.docstrings.prompt_builder",
            "v2.backend.core.docstrings.promp_api",
            "v2.backend.core.docstrings.prompts",
            "v2.backend.core.docstrings.builder",
        ),
        ("build_prompts_v1", "prompts_build_v1", "run_v1", "build_v1", "build"),
    ),
    # Docstrings: sanitize + verify
    "docstrings.sanitize.v1": (
        ("v2.backend.core.docstrings.providers", "v2.backend.core.docstrings.sanitize"),
        ("sanitize_outputs_v1", "run_v1", "sanitize"),
    ),
    # In _CAP_MAP (keep other entries as you have them)
    # in _CAP_MAP
    # in _CAP_MAP
    "docstrings.verify.v1": (
        ("v2.backend.core.docstrings.verify", "v2.backend.core.docstrings.providers"),
        ("verify_batch_v1", "verify_v1", "verify"),
    ),



    # Patch
    "patch.apply_files.v1": (
        ("v2.backend.core.patch_engine.providers",),
        ("apply_files_v1", "run_v1", "apply"),
    ),
}

def capability_run(name: str, payload: Any, context: Dict[str, Any]) -> List[Artifact]:
    """
    Execute a pipeline capability by name.
      1) Try spine.registry.run(name, payload, context)
      2) Else import a provider from _CAP_MAP (tries multiple modules) and call it with a TaskShim.
      3) If the provider is still missing and the capability is 'docstrings.build_prompts.v1',
         run a built-in fallback builder so the pipeline can continue.
    """
    # 1) Prefer registry
    res = _registry_run(name, payload, context)
    if isinstance(res, list):
        return res

    # 2) Fallback to direct import (multi-module tolerant)
    mapping = _CAP_MAP.get(name)
    if not mapping:
        return _artifact_problem(f"spine://capability/{name}", "CapabilityMissing",
                                 f"No mapping for capability '{name}'", retryable=False)

    modules, candidates = mapping
    fn, mod = _import_from_any(modules, candidates)

    # 3) Built-in fallback for the build phase when provider is truly missing
    if not callable(fn) and name == "docstrings.build_prompts.v1":
        # ensure we have a dict payload
        p = payload if isinstance(payload, dict) else {"payload": payload}
        return _fallback_build_prompts(p)

    if not callable(fn):
        return _artifact_problem(
            f"spine://capability/{name}",
            "ProviderMissing",
            f"Provider not found for '{name}'",
            details={"modules": modules, "candidates": candidates},
        )

    task = payload if hasattr(payload, "payload") else _TaskShim(payload if isinstance(payload, dict) else {"payload": payload})

    try:
        try:
            return fn(task, context)  # type: ignore[misc]
        except TypeError:
            return fn(task)  # type: ignore[misc]
    except Exception as e:
        return _artifact_problem(f"spine://capability/{name}", "ProviderError",
                                 f"{type(e).__name__}: {e}", retryable=False, details={})


def build_prompts_v1(task, context=None):
    """
    Generic prompt-builder for capability: prompts.build.v1.

    Order:
      1) Try an override capability (payload/env or default "prompts.build.override.v1").
      2) Try a generic implementation hook "prompts.build.impl.v1".
      3) Fall back to the internal generic builder (_fallback_build_prompts).

    No domain-specific names appear here; composition is done in YAML.
    """
    # Normalize payload to a dict
    if isinstance(task, dict):
        payload = task
    else:
        payload = (
            getattr(task, "payload", None)
            or getattr(task, "meta", None)
            or getattr(task, "data", None)
            or {}
        )

    # Allow per-run override without code changes
    override_cap = (
        payload.get("prompts_builder_capability")
        or os.environ.get("PROMPTS_BUILDER_CAPABILITY")
        or "prompts.build.override.v1"
    )

    def _try(cap_name: str):
        try:
            return capability_run(cap_name, payload, context or {})
        except Exception as e:
            # If not registered, fall through; if registered but failed, surface a Problem.
            msg = str(e)
            if "No provider registered" in msg or "No provider" in msg:
                return None
            return _artifact_problem(
                "spine://capability/prompts.build.v1",
                "ProviderError",
                f"{type(e).__name__}: {e}",
            )

    # 1) Override hook (YAML-wired)
    out = _try(override_cap)
    if out is not None:
        return out

    # 2) Generic implementation hook
    out = _try("prompts.build.impl.v1")
    if out is not None:
        return out

    # 3) Fallback builder in this module
    return _fallback_build_prompts(payload)


