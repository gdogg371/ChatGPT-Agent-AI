from __future__ import annotations

import ast
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = ["static_check_scan", "scan"]

FAMILY = "static"  # canonical family name we’ll emit


# ──────────────────────────────────────────────────────────────────────────────
# Issue model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Issue:
    path: str
    line: int
    col: int
    severity: str          # "error" | "warning" | "info"
    check: str             # machine name (e.g., "bare-except")
    code: str              # stable code (e.g., "ST001")
    message: str           # human-readable
    symbol: Optional[str] = None
    target: Optional[str] = None
    details: Optional[Dict] = None

    def to_record(self) -> Dict:
        rec: Dict = {
            "family": FAMILY,
            "kind": "static.issue",
            "path": self.path,
            "line": int(self.line),
            "col": int(self.col),
            "severity": self.severity,
            "check": self.check,
            "code": self.code,
            "message": self.message,
        }
        if self.symbol is not None:
            rec["symbol"] = self.symbol
        if self.target is not None:
            rec["target"] = self.target
        if self.details:
            rec["details"] = self.details
        return rec


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

_LONG_LINE_LIMIT = 120
_TODO_RE = re.compile(r"\b(TODO|FIXME|XXX)\b", re.IGNORECASE)

def _rel_posix(p: Path, repo_root: Path) -> str:
    try:
        return p.resolve().relative_to(repo_root.resolve()).as_posix()
    except Exception:
        return str(p).replace("\\", "/")

def _read_text(path: Path) -> str:
    # Be tolerant to odd encodings; preserve line positions.
    with path.open("rb") as f:
        raw = f.read()
    return io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", errors="replace").read()


# ──────────────────────────────────────────────────────────────────────────────
# Line-based checks
# ──────────────────────────────────────────────────────────────────────────────

def _scan_lines(abs_path: Path, rel: str, issues: List[Issue]) -> None:
    text = _read_text(abs_path)
    for i, line in enumerate(text.splitlines(), start=1):
        # long lines
        if len(line) > _LONG_LINE_LIMIT:
            issues.append(Issue(
                path=rel, line=i, col=_LONG_LINE_LIMIT + 1,
                severity="info", check="long-line", code="ST100",
                message=f"Line exceeds {_LONG_LINE_LIMIT} characters ({len(line)})."
            ))
        # trailing whitespace
        if len(line) and line.rstrip() != line:
            issues.append(Issue(
                path=rel, line=i, col=len(line),
                severity="info", check="trailing-whitespace", code="ST101",
                message="Trailing whitespace."
            ))
        # hard tabs
        if line.startswith("\t"):
            issues.append(Issue(
                path=rel, line=i, col=1,
                severity="info", check="hard-tab", code="ST102",
                message="Indentation uses hard tab."
            ))
        # TODO/FIXME/XXX
        if _TODO_RE.search(line):
            # Take first match column; stay robust if not found
            m = _TODO_RE.search(line)
            col = 1 + (m.start() if m else 0)
            issues.append(Issue(
                path=rel, line=i, col=col,
                severity="info", check="todo-comment", code="ST200",
                message="Contains TODO/FIXME/XXX comment."
            ))


# ──────────────────────────────────────────────────────────────────────────────
# AST-based checks
# ──────────────────────────────────────────────────────────────────────────────

class _ASTVisitor(ast.NodeVisitor):
    def __init__(self, rel: str, issues: List[Issue]) -> None:
        self.rel = rel
        self.issues = issues

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        # Bare except
        if node.type is None:
            self.issues.append(Issue(
                path=self.rel, line=getattr(node, "lineno", 1), col=getattr(node, "col_offset", 0) + 1,
                severity="error", check="bare-except", code="ST001",
                message="Bare except: clause."
            ))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Wildcard import
        if any(getattr(alias, "name", "") == "*" for alias in node.names):
            self.issues.append(Issue(
                path=self.rel, line=getattr(node, "lineno", 1), col=getattr(node, "col_offset", 0) + 1,
                severity="warning", check="wildcard-import", code="ST002",
                message="Wildcard import (from ... import *)."
            ))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        name = None
        if isinstance(fn, ast.Name):
            name = fn.id
        elif isinstance(fn, ast.Attribute):
            name = fn.attr

        # eval / exec usage
        if name in ("eval", "exec"):
            self.issues.append(Issue(
                path=self.rel, line=getattr(node, "lineno", 1), col=getattr(node, "col_offset", 0) + 1,
                severity="error", check=f"use-of-{name}", code="ST003",
                message=f"Use of {name}()."
            ))

        # print calls — informational (often undesirable in lib code)
        if isinstance(fn, ast.Name) and fn.id == "print":
            self.issues.append(Issue(
                path=self.rel, line=getattr(node, "lineno", 1), col=getattr(node, "col_offset", 0) + 1,
                severity="info", check="print-call", code="ST004",
                message="Use of print() call."
            ))

        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.issues.append(Issue(
            path=self.rel, line=getattr(node, "lineno", 1), col=getattr(node, "col_offset", 0) + 1,
            severity="info", check="global-statement", code="ST005",
            message=f"Use of 'global' for names: {', '.join(node.names)}."
        ))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Mutable default args
        for d in node.args.defaults:
            if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                self.issues.append(Issue(
                    path=self.rel, line=getattr(d, "lineno", getattr(node, "lineno", 1)),
                    col=getattr(d, "col_offset", getattr(node, "col_offset", 0)) + 1,
                    severity="warning", check="mutable-default-arg", code="ST006",
                    message="Mutable default argument."
                ))
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # same checks


def _scan_ast(abs_path: Path, rel: str, issues: List[Issue]) -> None:
    try:
        text = _read_text(abs_path)
        tree = ast.parse(text)
    except Exception:
        # Tolerate parse errors; a separate syntax scanner could flag them if desired
        return
    _ASTVisitor(rel, issues).visit(tree)


# ──────────────────────────────────────────────────────────────────────────────
# Public API (wired by your orchestrator)
# ──────────────────────────────────────────────────────────────────────────────

def static_check_scan(repo_root: Path, discovered_repo: Iterable[Tuple[Path, str]]) -> List[Dict]:
    """
    Scanner entrypoint expected by your read_scanners.py wiring.

    Parameters
    ----------
    repo_root : Path
        Absolute repository root (as provided by cfg.source_root).
    discovered_repo : Iterable[Tuple[Path, str]]
        Sequence of (absolute_file_path, repo_relative_posix) pairs from your discoverer.

    Returns
    -------
    List[Dict]
        Manifest-ready records, one per issue:
        {
          "family": "static",
          "kind": "static.issue",
          "path": "<repo-rel posix>",
          "line": int,
          "col": int,
          "severity": "error"|"warning"|"info",
          "check": "<short-name>",
          "code": "STnnn",
          "message": "<text>",
          # optional: "symbol", "target", "details"
        }
    """
    repo_root = Path(repo_root).resolve()
    issues: List[Issue] = []

    for abs_path, rel in discovered_repo:
        # Filter to Python only (mirror other scanners’ convention)
        if not str(abs_path).lower().endswith(".py"):
            continue

        # Ensure rel is a POSIX repo-relative path
        if not rel:
            rel = _rel_posix(abs_path, repo_root)

        # Run checks
        _scan_lines(abs_path, rel, issues)
        _scan_ast(abs_path, rel, issues)

    return [it.to_record() for it in issues]


# Maintain compatibility if someone imports scan()
scan = static_check_scan
