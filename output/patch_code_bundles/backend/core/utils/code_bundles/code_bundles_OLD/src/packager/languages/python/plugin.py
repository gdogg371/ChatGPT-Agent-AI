from __future__ import annotations

from typing import Dict, List, Tuple, Any
from pathlib import Path
import hashlib
import sys

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
    import graphs as g           # type: ignore

# --- FileRec shim (matches bundle_io.FileRec shape) ---
try:
    from bundle_io import FileRec  # path: str, data: bytes, sha256: str
except Exception:
    from dataclasses import dataclass
    @dataclass(frozen=True)
    class FileRec:  # type: ignore
        path: str
        data: bytes
        sha256: str


class PythonAnalyzer:
    """
    Adapter that runs Python analyses and returns bundle-ready artifacts.

    Input:  files = [(emitted_path, bytes), ...]
    Output: dict mapping artifact paths -> JSON-serializable payloads
    """

    def analyze(self, files: List[Tuple[str, bytes]]) -> Dict[str, Any]:
        # Convert incoming tuples to FileRec objects expected by analyzers
        frs: List[FileRec] = []
        for path, data in files:
            try:
                frs.append(FileRec(path=path, data=data, sha256=hashlib.sha256(data).hexdigest()))
            except Exception:
                # best-effort: skip malformed entry
                continue

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
        }
