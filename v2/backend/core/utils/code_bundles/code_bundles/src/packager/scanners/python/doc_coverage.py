# File: v2/backend/core/utils/code_bundles/code_bundles/doc_coverage.py
"""
Docstring coverage scanner (stdlib-only).

Emits JSONL-ready records:

Per-file:
  {
    "kind": "docs.coverage",
    "path": "v2/backend/…/module.py",
    "module_doc": true|false,
    "classes": {"total": N, "with_doc": M, "coverage": 0.0..1.0 | null},
    "methods": {"total": N, "with_doc": M, "coverage": 0.0..1.0 | null},
    "functions": {"total": N, "with_doc": M, "coverage": 0.0..1.0 | null},
    "overall": {"documentables": N, "with_doc": M, "coverage": 0.0..1.0 | null}
  }

Summary (across all scanned .py files):
  {
    "kind": "docs.coverage.summary",
    "files": <count>,
    "totals": {
      "modules": {"total": <files>, "with_doc": <count>, "coverage": 0.0..1.0 | null},
      "classes": {"total": N, "with_doc": M, "coverage": 0.0..1.0 | null},
      "methods": {"total": N, "with_doc": M, "coverage": 0.0..1.0 | null},
      "functions": {"total": N, "with_doc": M, "coverage": 0.0..1.0 | null},
      "overall": {"documentables": N, "with_doc": M, "coverage": 0.0..1.0 | null}
    }
  }

Notes
-----
* Paths returned are repo-relative POSIX. If your pipeline distinguishes
  local vs GitHub path modes, map `path` with your existing mapper before
  appending to the manifest (consistent with other scanners).
* Coverage is `None` (serialized as null) when the denominator is 0.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


# Public type used by the runner to pass discovered files
RepoItem = Tuple[Path, str]  # (local_path, repo_relative_posix)


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Counts:
    total: int = 0
    with_doc: int = 0

    def coverage(self) -> Optional[float]:
        if self.total <= 0:
            return None
        return self.with_doc / float(self.total)

def _has_doc(node: ast.AST) -> bool:
    # ast.get_docstring returns None if missing; treat empty/whitespace as False
    doc = ast.get_docstring(node, clean=False)
    if doc is None:
        return False
    return bool(str(doc).strip())

def _safe_parse(p: Path) -> Optional[ast.Module]:
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    try:
        return ast.parse(text)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Public API: per-file analysis
# ──────────────────────────────────────────────────────────────────────────────

def analyze_file(*, local_path: Path, repo_rel_posix: str) -> Optional[Dict]:
    """
    Analyze a single Python file for docstring coverage.

    Returns a record suitable for the manifest, or None if file cannot be parsed.
    """
    tree = _safe_parse(local_path)
    if tree is None:
        # Could not parse; still return a minimal record with zeros
        return {
            "kind": "docs.coverage",
            "path": repo_rel_posix,
            "module_doc": False,
            "classes": {"total": 0, "with_doc": 0, "coverage": None},
            "methods": {"total": 0, "with_doc": 0, "coverage": None},
            "functions": {"total": 0, "with_doc": 0, "coverage": None},
            "overall": {"documentables": 0, "with_doc": 0, "coverage": None},
        }

    module_has = _has_doc(tree)

    classes = Counts()
    methods = Counts()
    functions = Counts()

    # Walk only top-level defs for functions, and all class defs for methods
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.total += 1
            if _has_doc(node):
                functions.with_doc += 1
        elif isinstance(node, ast.ClassDef):
            classes.total += 1
            if _has_doc(node):
                classes.with_doc += 1

            # Count methods inside the class body
            for cnode in node.body:
                if isinstance(cnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.total += 1
                    if _has_doc(cnode):
                        methods.with_doc += 1

    # Overall: documentables = module + classes + methods + top-level functions
    doc_total = (1 if True else 0) + classes.total + methods.total + functions.total
    doc_with = (1 if module_has else 0) + classes.with_doc + methods.with_doc + functions.with_doc

    overall_cov: Optional[float]
    if doc_total > 0:
        overall_cov = doc_with / float(doc_total)
    else:
        overall_cov = None

    rec = {
        "kind": "docs.coverage",
        "path": repo_rel_posix,
        "module_doc": bool(module_has),
        "classes": {"total": classes.total, "with_doc": classes.with_doc, "coverage": classes.coverage()},
        "methods": {"total": methods.total, "with_doc": methods.with_doc, "coverage": methods.coverage()},
        "functions": {"total": functions.total, "with_doc": functions.with_doc, "coverage": functions.coverage()},
        "overall": {"documentables": doc_total, "with_doc": doc_with, "coverage": overall_cov},
    }
    return rec


# ──────────────────────────────────────────────────────────────────────────────
# Public API: bulk scan with summary
# ──────────────────────────────────────────────────────────────────────────────

def scan(repo_root: Path, discovered: Iterable[RepoItem]) -> List[Dict]:
    """
    Scan all discovered .py files and return a list of manifest records:
      - One 'docs.coverage' record per file
      - One 'docs.coverage.summary' record at the end

    Paths inside records are repo-relative POSIX. If your caller needs
    local/GitHub specific path mapping, apply it before appending.
    """
    files: List[RepoItem] = [(lp, rel) for (lp, rel) in discovered if rel.endswith(".py")]

    results: List[Dict] = []
    # Summary accumulators
    modules_total = 0
    modules_with = 0
    classes_total = 0
    classes_with = 0
    methods_total = 0
    methods_with = 0
    functions_total = 0
    functions_with = 0
    overall_doc_total = 0
    overall_doc_with = 0

    for local, rel in files:
        rec = analyze_file(local_path=local, repo_rel_posix=rel)
        if rec is None:
            continue
        results.append(rec)

        # aggregate
        modules_total += 1
        if rec.get("module_doc"):
            modules_with += 1

        c = rec.get("classes", {})
        m = rec.get("methods", {})
        f = rec.get("functions", {})
        o = rec.get("overall", {})

        classes_total += int(c.get("total", 0))
        classes_with += int(c.get("with_doc", 0))

        methods_total += int(m.get("total", 0))
        methods_with += int(m.get("with_doc", 0))

        functions_total += int(f.get("total", 0))
        functions_with += int(f.get("with_doc", 0))

        overall_doc_total += int(o.get("documentables", 0))
        overall_doc_with += int(o.get("with_doc", 0))

    def cov(num: int, den: int) -> Optional[float]:
        return (num / float(den)) if den > 0 else None

    summary = {
        "kind": "docs.coverage.summary",
        "files": len(files),
        "totals": {
            "modules": {"total": modules_total, "with_doc": modules_with, "coverage": cov(modules_with, modules_total)},
            "classes": {"total": classes_total, "with_doc": classes_with, "coverage": cov(classes_with, classes_total)},
            "methods": {"total": methods_total, "with_doc": methods_with, "coverage": cov(methods_with, methods_total)},
            "functions": {
                "total": functions_total,
                "with_doc": functions_with,
                "coverage": cov(functions_with, functions_total),
            },
            "overall": {
                "documentables": overall_doc_total,
                "with_doc": overall_doc_with,
                "coverage": cov(overall_doc_with, overall_doc_total),
            },
        },
    }
    results.append(summary)
    return results


__all__ = ["scan", "analyze_file"]
