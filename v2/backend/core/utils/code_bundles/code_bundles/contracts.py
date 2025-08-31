# v2/backend/core/utils/code_bundles/code_bundles/contracts.py

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Mapping, Optional, List


def build_manifest_header(
    *,
    manifest_version: str,
    generated_at: str,
    source_root: str,
    include_globs: list,
    exclude_globs: list,
    segment_excludes: list,
    case_insensitive: bool,
    follow_symlinks: bool,
    modes: Mapping[str, bool],
    tool_versions: Mapping[str, str],
) -> Dict[str, Any]:
    """
    Create (or validate) the header record for the JSONL design manifest.
    """
    return {
        "record_type": "manifest_header",
        "manifest_version": str(manifest_version),
        "generated_at": str(generated_at),
        "source_root": str(source_root),
        "scan_rules": {
            "include_globs": list(include_globs or []),
            "exclude_globs": list(exclude_globs or []),
            "segment_excludes": list(segment_excludes or []),
            "case_insensitive": bool(case_insensitive),
            "follow_symlinks": bool(follow_symlinks),
        },
        "modes": {"local": bool(modes.get("local")), "github": bool(modes.get("github"))},
        "tool_versions": dict(tool_versions or {}),
    }


def build_bundle_summary(*, counts: Mapping[str, int], durations_ms: Mapping[str, int]) -> Dict[str, Any]:
    """
    Terminal summary record. Accepts arbitrary counters and duration buckets.
    """
    out = {
        "record_type": "bundle_summary",
        "counts": {k: int(v) for k, v in (counts or {}).items()},
        "durations_ms": {k: int(v) for k, v in (durations_ms or {}).items()},
    }
    return out


# Optional helpers for AST records (not required by the runner, but kept for reference)

def ast_symbol(*, path: str, module: str, name: str, kind: str, lineno: int, end_lineno: int, **kwargs) -> Dict[str, Any]:
    rec = {
        "record_type": "ast.symbol",
        "path": path,
        "module": module,
        "name": name,
        "kind": kind,
        "lineno": int(lineno),
        "end_lineno": int(end_lineno),
    }
    rec.update(kwargs or {})
    return rec


def ast_call(*, path: str, module: str, caller_name: str, callee: str, lineno: int, end_lineno: int) -> Dict[str, Any]:
    return {
        "record_type": "ast.call",
        "path": path,
        "module": module,
        "caller_name": caller_name,
        "callee": callee,
        "lineno": int(lineno),
        "end_lineno": int(end_lineno),
    }


def ast_xref(*, path: str, module: str, kind: str, name: str, lineno: int, **kwargs) -> Dict[str, Any]:
    rec = {
        "record_type": "ast.xref",
        "path": path,
        "module": module,
        "kind": kind,
        "name": name,
        "lineno": int(lineno),
    }
    rec.update(kwargs or {})
    return rec


def build_quality_metric(
    *, path: str, language: str, sloc: int, loc: int,
    cyclomatic: int, n_functions: int, n_classes: int,
    avg_fn_len: float, notes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "kind": "quality.metric",
        "path": path,
        "language": language,
        "sloc": int(sloc),
        "loc": int(loc),
        "cyclomatic": int(cyclomatic),
        "n_functions": int(n_functions),
        "n_classes": int(n_classes),
        "avg_fn_len": float(avg_fn_len),
        "notes": notes or None,
    }
