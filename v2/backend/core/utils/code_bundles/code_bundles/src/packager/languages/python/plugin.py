# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/languages/python/plugin.py
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple, Union, Optional

# ---- Public plugin contract --------------------------------------------------
# The loader expects a module-level `PLUGIN` object exposing:
#   - name: str
#   - extensions: Tuple[str, ...]
#   - analyze(files) -> Dict[str, Any]   # returns {artifact_path: json-serializable}
#
# `files` is typically a sequence of (rel_path: str, data: bytes) tuples.
# This plugin also accepts {path: bytes} dicts or sequences of paths (it’ll read from disk).

PLUGIN_NAME = "python"
EXTENSIONS: Tuple[str, ...] = (".py", ".pyi")

BytesLike = Union[bytes, bytearray, memoryview]


# ---- Internal helpers --------------------------------------------------------
@dataclass(frozen=True)
class FileEntry:
    path: str
    data: bytes


def _is_py(p: str) -> bool:
    p = str(p)
    return any(p.endswith(ext) for ext in EXTENSIONS)


def _coerce_files(files: Any) -> List[FileEntry]:
    """
    Accepts:
      - Sequence[Tuple[str, bytes]]  (preferred)
      - Dict[str, bytes]
      - Sequence[str | Path]         (fallback: reads from disk)
    Filters to Python extensions.
    """
    out: List[FileEntry] = []

    if isinstance(files, dict):
        for p, b in files.items():
            if _is_py(p):
                out.append(FileEntry(str(p), bytes(b)))
        return out

    if isinstance(files, Sequence):
        for item in files:
            if isinstance(item, tuple) and len(item) == 2:
                p, b = item
                if _is_py(p):
                    out.append(FileEntry(str(p), bytes(b)))
            elif isinstance(item, (str, Path)):
                p = str(item)
                if _is_py(p):
                    try:
                        out.append(FileEntry(p, Path(p).read_bytes()))
                    except Exception:
                        out.append(FileEntry(p, b""))
        return out

    # Unknown shape -> nothing
    return out


def _safe_decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except Exception:
        return data.decode("utf-8", "replace")


def _analyze_one(path: str, src: str) -> Dict[str, Any]:
    """
    Lightweight AST scan:
      - LOC
      - function/class counts
      - imported modules (unique, sorted)
    """
    report: Dict[str, Any] = {
        "path": path,
        "loc": src.count("\n") + (1 if src and not src.endswith("\n") else 0),
        "functions": 0,
        "classes": 0,
        "imports": [],
        "errors": None,
    }

    try:
        tree = ast.parse(src, filename=path)
        funcs = 0
        classes = 0
        imports: List[str] = []

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs += 1
            elif isinstance(node, ast.ClassDef):
                classes += 1
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name:
                        imports.append(a.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod:
                    imports.append(mod)

        report["functions"] = funcs
        report["classes"] = classes
        report["imports"] = sorted(set(i for i in imports if i))

    except Exception as e:
        report["errors"] = repr(e)

    return report


# ---- Plugin implementation ---------------------------------------------------
class _PythonPlugin:
    name = PLUGIN_NAME
    extensions = EXTENSIONS

    def analyze(self, files: Any) -> Dict[str, Any]:
        entries = _coerce_files(files)

        file_reports: List[Dict[str, Any]] = []
        total_loc = 0
        total_funcs = 0
        total_classes = 0

        for fe in entries:
            src = _safe_decode(fe.data)
            rep = _analyze_one(fe.path, src)
            file_reports.append(rep)
            total_loc += int(rep.get("loc", 0) or 0)
            total_funcs += int(rep.get("functions", 0) or 0)
            total_classes += int(rep.get("classes", 0) or 0)

        summary = {
            "plugin": self.name,
            "version": 1,
            "files": len(entries),
            "total_loc": total_loc,
            "total_functions": total_funcs,
            "total_classes": total_classes,
        }

        # Artifacts mapping: {relative_output_path: JSON-serializable object}
        return {
            "python/index.json": {
                "summary": summary,
                "files": file_reports,
            }
        }


PLUGIN = _PythonPlugin()
__all__ = ["PLUGIN"]

