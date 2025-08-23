# File: backend/core/utils/code_bundles/code_bundles_v2/src/packager/languages/python/plugin.py
from __future__ import annotations

"""
Python language plugin (v2)

Extends analysis to:
- Validate Python syntax (including .pyi stubs) and emit a syntax report.
- Continue producing existing artifacts via the legacy analyzers (python_index, graphs).

Inputs:
    files: List[Tuple[str, bytes]]   # emitted path, raw bytes

Outputs (paths are inside the bundle):
    analysis/ldt.json
    analysis/blocks.json
    analysis/typed_ast.json
    analysis/tokens.json
    graphs/symbols.json
    graphs/imports.json
    graphs/calls.json
    analysis/python_syntax.json      # NEW: per-file syntax validation report
"""

from dataclasses import dataclass
from typing import Dict, List, Tuple, Any
from pathlib import Path
import hashlib
import sys
import ast

# This plugin handles both .py and .pyi
EXTENSIONS = (".py", ".pyi")
PLUGIN_NAME = "python"


# --- ensure repo root is importable so top-level helpers resolve ---
def _add_repo_root_to_syspath() -> None:
    here = Path(__file__).resolve()
    root = None
    for p in here.parents:
        if p.name == "src":
            root = p.parent
            break
    if root and str(root) not in sys.path:
        sys.path.insert(0, str(root))


try:
    import python_index as pidx
    import graphs as g
except Exception:
    _add_repo_root_to_syspath()
    import python_index as pidx  # type: ignore
    import graphs as g  # type: ignore


# --- FileRec shim (matches bundle_io.FileRec shape) ---
try:
    from bundle_io import FileRec  # path: str, data: bytes, sha256: str
except Exception:
    @dataclass(frozen=True)
    class FileRec:  # type: ignore
        path: str
        data: bytes
        sha256: str


class PythonAnalyzer:
    """
    Adapter that runs Python analyses and returns bundle-ready artifacts.
    """

    def _syntax_report(self, files: List[Tuple[str, bytes]]) -> Dict[str, Any]:
        """
        Try to decode as UTF-8 and parse via ast.parse().
        We treat .py and .pyi identically for parsing purposes.
        """
        out: List[Dict[str, Any]] = []
        for path, data in files:
            rec: Dict[str, Any] = {"path": path, "ok": False}
            try:
                text = data.decode("utf-8")
            except Exception as e:
                rec["error"] = f"decode: {type(e).__name__}: {e}"
                out.append(rec)
                continue
            try:
                ast.parse(text, filename=path, mode="exec")
                rec["ok"] = True
            except Exception as e:
                rec["error"] = f"parse: {type(e).__name__}: {e}"
            out.append(rec)
        return {"version": "1", "files": out}

    def analyze(self, files: List[Tuple[str, bytes]]) -> Dict[str, Any]:
        # Convert incoming tuples to FileRec objects expected by analyzers
        frs: List[FileRec] = []
        for path, data in files:
            try:
                frs.append(FileRec(path=path, data=data, sha256=hashlib.sha256(data).hexdigest()))
            except Exception:
                # best-effort: skip malformed entry
                continue

        # NEW: Emit a syntax validation report upfront
        syntax = self._syntax_report(files)

        # Run analyses (python_index & graphs consume List[FileRec])
        ldt = pidx.build_ldt(frs)
        blocks = pidx.build_block_index(frs)
        typed_ast = pidx.dump_typed_ast(frs)
        tokens = pidx.dump_tokens(frs)
        symbols = g.build_symbol_table(frs)
        imports = g.build_import_graph(frs)
        calls = g.build_call_graph(frs, symbols)

        return {
            "analysis/ldt.json": ldt,
            "analysis/blocks.json": blocks,
            "analysis/typed_ast.json": typed_ast,
            "analysis/tokens.json": tokens,
            "graphs/symbols.json": symbols,
            "graphs/imports.json": imports,
            "graphs/calls.json": calls,
            "analysis/python_syntax.json": syntax,  # NEW
        }


# ---- PLUGIN object for the generic loader ----

class _PythonPlugin:
    name = PLUGIN_NAME
    extensions = EXTENSIONS

    def analyze(self, files: List[Tuple[str, bytes]]):
        return PythonAnalyzer().analyze(files)


PLUGIN = _PythonPlugin()
