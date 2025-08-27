# python_index.py
from __future__ import annotations

import ast
import hashlib
import io
import tokenize
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from bundle_io import FileRec


@dataclass
class _Block:
    kind: str            # "function" | "class"
    name: str
    qualname: str
    parent: Optional[str]
    start_line: int
    end_line: int
    docstring_present: bool
    signature: Optional[str]
    decorators: List[str]
    sha256_block: str


class _BlockVisitor(ast.NodeVisitor):
    """Collect classes/functions, signatures, decorators, and block hash."""
    def __init__(self, src_text: str) -> None:
        self._src_lines = src_text.splitlines(keepends=True)
        self.blocks: List[Dict[str, Any]] = []
        self._stack: List[str] = []

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "function" if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else "class"
            name = getattr(node, "name", "?")
            parent = ".".join(self._stack) if self._stack else None
            qual = f"{parent}.{name}" if parent else name

            start = getattr(node, "lineno", 1)
            end = getattr(node, "end_lineno", start)

            doc_present = bool(ast.get_docstring(node, clean=False))

            # Decorators → strings (best-effort)
            decorators: List[str] = []
            for d in getattr(node, "decorator_list", []):
                try:
                    decorators.append(ast.unparse(d))  # 3.9+
                except Exception:
                    if hasattr(d, "id"):
                        decorators.append(getattr(d, "id"))
                    elif hasattr(d, "attr"):
                        decorators.append(getattr(d, "attr"))
                    else:
                        decorators.append("")

            # Function signature (names only)
            signature: Optional[str] = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                try:
                    signature = "(" + ", ".join(a.arg for a in node.args.args) + ")"
                except Exception:
                    signature = None

            try:
                slice_text = "".join(self._src_lines[start - 1 : end])
                blk_sha = hashlib.sha256(slice_text.encode("utf-8")).hexdigest()
            except Exception:
                blk_sha = ""

            self.blocks.append(
                _Block(
                    kind=kind,
                    name=name,
                    qualname=qual,
                    parent=parent,
                    start_line=start,
                    end_line=end,
                    docstring_present=doc_present,
                    signature=signature,
                    decorators=decorators,
                    sha256_block=blk_sha,
                ).__dict__
            )

            self._stack.append(name)
            super().generic_visit(node)
            self._stack.pop()
        else:
            super().generic_visit(node)


class PythonIndexer:
    """
    Class-based indexer that mirrors the original module’s behavior:
      - build_ldt(files)           → line digest table
      - build_block_index(files)   → class/function blocks
      - dump_typed_ast(files)      → typed AST node spans
      - dump_tokens(files)         → lexical tokens
    """

    # ---------- public API ----------

    def build_ldt(self, files: List[FileRec]) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        for fr in files:
            txt = self._safe_text(fr)
            if txt is None:
                continue
            byte_pos = 0
            for i, line in enumerate(txt.splitlines(keepends=True), start=1):
                b = line.encode("utf-8")
                sha = hashlib.sha256(b).hexdigest()
                sha_norm = hashlib.sha256(line.rstrip().encode("utf-8")).hexdigest()
                rows.append(
                    {
                        "file_path": fr.path,
                        "file_sha256": fr.sha256,
                        "n": i,
                        "byte_start": byte_pos,
                        "byte_end": byte_pos + len(b),
                        "sha256_line": sha,
                        "sha256_line_normalized": sha_norm,
                        "is_blank": (line.strip() == ""),
                        "is_comment": line.lstrip().startswith("#"),
                    }
                )
                byte_pos += len(b)
        return {"rows": rows}

    def build_block_index(self, files: List[FileRec]) -> Dict[str, Any]:
        out: List[Dict[str, Any]] = []
        for fr in files:
            if not self._is_python(fr.path):
                continue
            src = self._safe_text(fr) or ""
            try:
                tree = ast.parse(src)
            except Exception:
                continue
            v = _BlockVisitor(src)
            v.visit(tree)
            out.append({"file_path": fr.path, "file_sha256": fr.sha256, "blocks": v.blocks})
        return {"files": out}

    def dump_typed_ast(self, files: List[FileRec]) -> Dict[str, Any]:
        out: List[Dict[str, Any]] = []
        for fr in files:
            if not self._is_python(fr.path):
                continue
            src = self._safe_text(fr) or ""
            try:
                tree = ast.parse(src)
            except Exception:
                continue
            nodes: List[Dict[str, Any]] = []
            counter = 0
            for node in ast.walk(tree):
                counter += 1
                nodes.append(
                    {
                        "id": counter,
                        "type": type(node).__name__,
                        "start_line": getattr(node, "lineno", None),
                        "start_col": getattr(node, "col_offset", None),
                        "end_line": getattr(node, "end_lineno", None),
                        "end_col": getattr(node, "end_col_offset", None),
                    }
                )
            out.append({"file_path": fr.path, "file_sha256": fr.sha256, "nodes": nodes})
        return {"files": out}

    def dump_tokens(self, files: List[FileRec]) -> Dict[str, Any]:
        out: List[Dict[str, Any]] = []
        for fr in files:
            if not self._is_python(fr.path):
                continue
            src = self._safe_text(fr)
            if src is None:
                continue
            toks: List[Dict[str, Any]] = []
            try:
                for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                    toks.append(
                        {
                            "type": tokenize.tok_name.get(tok.type, str(tok.type)),
                            "lexeme": tok.string,
                            "start_line": tok.start[0],
                            "start_col": tok.start[1],
                            "end_line": tok.end[0],
                            "end_col": tok.end[1],
                        }
                    )
            except Exception:
                continue
            out.append({"file_path": fr.path, "file_sha256": fr.sha256, "tokens": toks})
        return {"files": out}

    # ---------- internals ----------

    @staticmethod
    def _is_python(path: str) -> bool:
        return path.endswith(".py")

    @staticmethod
    def _safe_text(fr: FileRec) -> Optional[str]:
        try:
            return fr.data.decode("utf-8")
        except Exception:
            return None


# Single shared instance + legacy function shims (to avoid touching callers)
_INDEXER = PythonIndexer()

def build_ldt(files: List[FileRec]) -> Dict[str, Any]:
    return _INDEXER.build_ldt(files)

def build_block_index(files: List[FileRec]) -> Dict[str, Any]:
    return _INDEXER.build_block_index(files)

def dump_typed_ast(files: List[FileRec]) -> Dict[str, Any]:
    return _INDEXER.dump_typed_ast(files)

def dump_tokens(files: List[FileRec]) -> Dict[str, Any]:
    return _INDEXER.dump_tokens(files)
