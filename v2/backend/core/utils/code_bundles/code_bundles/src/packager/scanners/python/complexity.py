# File: v2/backend/core/utils/code_bundles/code_bundles/complexity.py
"""
Cyclomatic complexity scanner (stdlib-only).

Emits JSONL-ready records:

Per-file:
  {
    "kind": "quality.complexity",
    "path": "v2/backend/…/module.py",
    "functions": 12,
    "total_complexity": 37,
    "avg_complexity": 3.083,
    "max_complexity": 9,
    "p95_complexity": 7.8,
    "by_function": [
      {"name":"MyClass.method","lineno":42,"complexity":9},
      {"name":"helper","lineno":11,"complexity":6},
      ...
    ]
  }

Summary (across all scanned .py files):
  {
    "kind": "quality.complexity.summary",
    "files": <count>,
    "functions": <count>,
    "total_complexity": <sum>,
    "avg_complexity": 0.0..,
    "p95_function_complexity": 0.0..,
    "max_function_complexity": <int>,
    "heavy_files_top": [
      {"path":"…/a.py","total_complexity":37,"functions":12},
      ...
    ]
  }

Notes
-----
* Paths returned are repo-relative POSIX. If your pipeline distinguishes
  local vs GitHub path modes, map `path` with your existing mapper before
  appending to the manifest (consistent with other scanners).
* Cyclomatic complexity is approximated with common rules:
    - Base of 1 per function.
    - +1 per: If, For, AsyncFor, While, IfExp (ternary),
              each ExceptHandler, each 'and'/'or' boolean op beyond the first,
              each comprehension 'if', each Match case, each Assert, Lambda.
    - BoolOp adds (len(values)-1).
    - Try contributes the number of handlers (except blocks).
  This is a pragmatic, dependency-free estimator.
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

RepoItem = Tuple[Path, str]  # (local_path, repo_relative_posix)


# ──────────────────────────────────────────────────────────────────────────────
# Complexity core
# ──────────────────────────────────────────────────────────────────────────────

class _FuncComplexityVisitor(ast.NodeVisitor):
    """
    Visits a single function body and accumulates cyclomatic complexity.

    IMPORTANT: Does NOT descend into nested functions or classes; those are
    measured separately as their own functions.
    """
    def __init__(self) -> None:
        self.score = 1  # base complexity per function

    # Control flow branches
    def visit_If(self, node: ast.If) -> None:
        self.score += 1
        self.generic_visit_no_defs(node)

    def visit_For(self, node: ast.For) -> None:
        self.score += 1
        self.generic_visit_no_defs(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.score += 1
        self.generic_visit_no_defs(node)

    def visit_While(self, node: ast.While) -> None:
        self.score += 1
        self.generic_visit_no_defs(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.score += 1
        self.generic_visit_no_defs(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self.score += 1
        self.generic_visit_no_defs(node)

    # Boolean ops: a and b and c -> adds 2 (n-1)
    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        # len(values) >= 2 typically
        add = max(0, len(getattr(node, "values", [])) - 1)
        self.score += add
        self.generic_visit_no_defs(node)

    # Try/Except: each handler adds one
    def visit_Try(self, node: ast.Try) -> None:
        self.score += len(getattr(node, "handlers", []) or [])
        self.generic_visit_no_defs(node)

    # Comprehensions: add for each 'if' in each generator
    def visit_ListComp(self, node: ast.ListComp) -> None:
        for gen in node.generators:
            self.score += len(getattr(gen, "ifs", []) or [])
        self.generic_visit_no_defs(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        for gen in node.generators:
            self.score += len(getattr(gen, "ifs", []) or [])
        self.generic_visit_no_defs(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        for gen in node.generators:
            self.score += len(getattr(gen, "ifs", []) or [])
        self.generic_visit_no_defs(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        for gen in node.generators:
            self.score += len(getattr(gen, "ifs", []) or [])
        self.generic_visit_no_defs(node)

    # Match/case (Python 3.10+): each case is a branch
    def visit_Match(self, node: ast.Match) -> None:  # type: ignore[attr-defined]
        cases = getattr(node, "cases", []) or []
        self.score += len(cases)
        self.generic_visit_no_defs(node)

    # Lambda adds a branch (conditional logic often hides inside)
    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.score += 1
        self.generic_visit_no_defs(node)

    # Prevent descending into nested defs/classes (measured separately)
    def generic_visit_no_defs(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            super(_FuncComplexityVisitor, self).visit(child)


@dataclass
class _FuncInfo:
    name: str
    lineno: int
    complexity: int
    nested: bool = False  # whether function is nested inside another function


class _FunctionCollector(ast.NodeVisitor):
    """
    Collects functions/methods with their computed complexity.
    Tracks class/function nesting to produce readable names.
    """
    def __init__(self) -> None:
        self.class_stack: List[str] = []
        self.func_stack: List[str] = []
        self.items: List[_FuncInfo] = []

    def _qualname(self, func_name: str) -> str:
        parts: List[str] = []
        if self.class_stack:
            parts.extend(self.class_stack)
        parts.append(func_name)
        return ".".join(parts)

    def _compute_complexity(self, node: ast.AST) -> int:
        v = _FuncComplexityVisitor()
        v.generic_visit_no_defs(node)  # visit node body with no nested defs
        return v.score

    # Class: descend and collect methods
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_stack.append(node.name)
        try:
            for child in node.body:
                self.visit(child)
        finally:
            self.class_stack.pop()

    # Functions and async functions
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        cname = self._qualname(node.name)
        nested = len(self.func_stack) > 0
        score = self._compute_complexity(node)
        self.items.append(_FuncInfo(name=cname, lineno=getattr(node, "lineno", 0) or 0, complexity=score, nested=nested))
        # Recurse to catch nested functions (as their own items)
        self.func_stack.append(node.name)
        try:
            for child in node.body:
                self.visit(child)
        finally:
            self.func_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        cname = self._qualname(node.name)
        nested = len(self.func_stack) > 0
        score = self._compute_complexity(node)
        self.items.append(_FuncInfo(name=cname, lineno=getattr(node, "lineno", 0) or 0, complexity=score, nested=nested))
        # Recurse to catch nested functions
        self.func_stack.append(node.name)
        try:
            for child in node.body:
                self.visit(child)
        finally:
            self.func_stack.pop()


# ──────────────────────────────────────────────────────────────────────────────
# Per-file analysis & aggregation helpers
# ──────────────────────────────────────────────────────────────────────────────

def _safe_parse(p: Path):
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    try:
        return ast.parse(text)
    except Exception:
        return None

def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    pct = max(0.0, min(100.0, pct))
    if len(values) == 1:
        return float(values[0])
    xs = sorted(values)
    # nearest-rank method
    k = int(math.ceil((pct / 100.0) * len(xs))) - 1
    k = max(0, min(k, len(xs) - 1))
    return float(xs[k])

def _round3(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 3)


# ──────────────────────────────────────────────────────────────────────────────
# Public API: analyze a single file
# ──────────────────────────────────────────────────────────────────────────────

def analyze_file(*, local_path: Path, repo_rel_posix: str, top_n_functions: int = 100) -> Dict:
    """
    Analyze a Python file's functions/methods for cyclomatic complexity.
    Always returns a record; unparsable files produce zeros.
    """
    tree = _safe_parse(local_path)
    if tree is None:
        return {
            "kind": "quality.complexity",
            "path": repo_rel_posix,
            "functions": 0,
            "total_complexity": 0,
            "avg_complexity": None,
            "max_complexity": None,
            "p95_complexity": None,
            "by_function": [],
        }

    collector = _FunctionCollector()
    collector.visit(tree)
    funcs = collector.items

    counts = len(funcs)
    complexities = [fi.complexity for fi in funcs]

    total = int(sum(complexities)) if complexities else 0
    avg = (sum(complexities) / float(counts)) if counts > 0 else None
    mx = max(complexities) if complexities else None
    p95 = _percentile([float(c) for c in complexities], 95.0) if complexities else None

    # Top functions by complexity (stable order by -complexity, lineno)
    top = sorted(funcs, key=lambda fi: (-fi.complexity, fi.lineno))[: max(0, int(top_n_functions))]
    top_payload = [{"name": fi.name, "lineno": fi.lineno, "complexity": fi.complexity, "nested": fi.nested} for fi in top]

    return {
        "kind": "quality.complexity",
        "path": repo_rel_posix,
        "functions": counts,
        "total_complexity": total,
        "avg_complexity": _round3(avg),
        "max_complexity": mx,
        "p95_complexity": _round3(p95),
        "by_function": top_payload,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Public API: bulk scan with summary
# ──────────────────────────────────────────────────────────────────────────────

def scan(repo_root: Path, discovered: Iterable[RepoItem]) -> List[Dict]:
    """
    Scan all discovered .py files and return a list of manifest records:
      - One 'quality.complexity' record per file
      - One 'quality.complexity.summary' record at the end

    Paths inside records are repo-relative POSIX. If your caller needs
    local/GitHub specific path mapping, apply it before appending.
    """
    # Filter Python files
    files: List[RepoItem] = [(lp, rel) for (lp, rel) in discovered if rel.endswith(".py")]

    results: List[Dict] = []
    all_func_complexities: List[int] = []
    per_file_totals: List[Tuple[str, int, int]] = []  # (path, total_complexity, functions)

    for local, rel in files:
        rec = analyze_file(local_path=local, repo_rel_posix=rel)
        results.append(rec)

        per_file_totals.append((rel, int(rec.get("total_complexity", 0)), int(rec.get("functions", 0))))
        for f in rec.get("by_function", []):
            all_func_complexities.append(int(f.get("complexity", 0)))

    # Global summary
    total_functions = sum(f for _, _, f in per_file_totals)
    total_complexity = sum(t for _, t, _ in per_file_totals)
    avg_complexity = (total_complexity / float(total_functions)) if total_functions > 0 else None
    max_func_complexity = max(all_func_complexities) if all_func_complexities else None
    p95_func_complexity = _percentile([float(x) for x in all_func_complexities], 95.0) if all_func_complexities else None

    # Top heavy files by total complexity
    heavy_files_top = sorted(per_file_totals, key=lambda it: (-it[1], it[0]))[:20]
    heavy_payload = [{"path": p, "total_complexity": tc, "functions": fn} for (p, tc, fn) in heavy_files_top]

    summary = {
        "kind": "quality.complexity.summary",
        "files": len(files),
        "functions": total_functions,
        "total_complexity": int(total_complexity),
        "avg_complexity": _round3(avg_complexity),
        "p95_function_complexity": _round3(p95_func_complexity),
        "max_function_complexity": max_func_complexity,
        "heavy_files_top": heavy_payload,
    }
    results.append(summary)
    return results


__all__ = ["scan", "analyze_file"]
