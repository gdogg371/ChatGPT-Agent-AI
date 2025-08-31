# File: v2/backend/core/utils/code_bundles/code_bundles/quality.py
"""
Quality metrics extraction.

Currently supports Python files:
- loc (total lines)
- sloc (non-blank, non-comment-only lines)
- cyclomatic complexity (very basic AST-based estimate)
- number of functions/classes
- average function length in lines

On parse errors we still return a record with best-effort textual metrics
and attach notes=["parse_error"].
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from v2.backend.core.utils.code_bundles.code_bundles.contracts import (
    build_quality_metric,
)


def _text_loc_sloc(text: str) -> Tuple[int, int]:
    """
    Return (loc, sloc). sloc excludes blank lines and comment-only lines.
    """
    loc = 0
    sloc = 0
    for line in text.splitlines():
        loc += 1
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        sloc += 1
    return loc, sloc


# Nodes that increment cyclomatic complexity by 1
_COMPLEXITY_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.ExceptHandler,
    ast.BoolOp,       # and/or
    ast.IfExp,        # ternary
    ast.comprehension # comprehensions have implicit loops/ifs
)

def _cyclomatic_complexity(mod: ast.AST) -> int:
    """
    Very rough estimate: 1 (base) + count of branching constructs.
    """
    score = 1
    for node in ast.walk(mod):
        if isinstance(node, _COMPLEXITY_NODES):
            score += 1
    return score


def _function_spans(mod: ast.AST) -> List[int]:
    """
    Collect function lengths in lines using lineno/end_lineno when available.
    """
    spans: List[int] = []
    for node in ast.walk(mod):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = int(getattr(node, "lineno", 0) or 0)
            end = int(getattr(node, "end_lineno", 0) or 0)
            if end and start and end >= start:
                spans.append(end - start + 1)
    return spans


def quality_for_python(*, path: Path, repo_rel_posix: str) -> Dict[str, Any]:
    """
    Compute quality metrics for a Python file. Always returns a record.
    On unreadable files, returns minimal metrics with notes.
    """
    text: Optional[str] = None
    notes: List[str] = []

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        notes.append("decode_error")
    except Exception:
        notes.append("read_error")

    if text is None:
        # Cannot read; return minimal
        return build_quality_metric(
            path=repo_rel_posix,
            language="python",
            sloc=0,
            loc=0,
            cyclomatic=0,
            n_functions=0,
            n_classes=0,
            avg_fn_len=0.0,
            notes=notes or ["unreadable"],
        )

    loc, sloc = _text_loc_sloc(text)

    try:
        mod = ast.parse(text, filename=repo_rel_posix)
        cyclo = _cyclomatic_complexity(mod)
        n_functions = 0
        n_classes = 0
        for node in ast.walk(mod):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                n_functions += 1
            elif isinstance(node, ast.ClassDef):
                n_classes += 1
        spans = _function_spans(mod)
        avg_fn_len = (sum(spans) / len(spans)) if spans else 0.0
        return build_quality_metric(
            path=repo_rel_posix,
            language="python",
            sloc=sloc,
            loc=loc,
            cyclomatic=cyclo,
            n_functions=n_functions,
            n_classes=n_classes,
            avg_fn_len=round(avg_fn_len, 2),
            notes=notes or None,
        )
    except Exception:
        notes.append("parse_error")
        return build_quality_metric(
            path=repo_rel_posix,
            language="python",
            sloc=sloc,
            loc=loc,
            cyclomatic=0,
            n_functions=0,
            n_classes=0,
            avg_fn_len=0.0,
            notes=notes,
        )
