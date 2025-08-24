# graphs.py
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Set

from bundle_io import FileRec


@dataclass
class _Sym:
    file_path: str
    name: str
    kind: str            # "function" | "class" | "var"
    scope: str           # dotted path (e.g., "module", "Foo", "Foo.bar")
    line: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path,
            "name": self.name,
            "kind": self.kind,
            "scope": self.scope,
            "defined_at": {"line": self.line},
        }


class Graphs:
    """
    Class-based builders for:
      - Symbol table          → {"symbols":[...]}
      - Import graph          → {"nodes":[...], "edges":[...]}
      - Call graph            → {"nodes":[...], "edges":[...]}
    Output shapes match the original functions.
    """

    # ---------- public API ----------

    def build_symbol_table(self, files: List[FileRec]) -> Dict[str, Any]:
        symbols: List[_Sym] = []
        for fr in files:
            if not self._is_python(fr.path):
                continue
            src = self._safe_text(fr) or ""
            try:
                tree = ast.parse(src)
            except Exception:
                continue

            scope_stack: List[str] = ["module"]

            def add_sym(name: str, kind: str, line: int) -> None:
                symbols.append(
                    _Sym(file_path=fr.path, name=name, kind=kind, scope=".".join(scope_stack), line=line)
                )

            class V(ast.NodeVisitor):
                def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
                    add_sym(node.name, "function", node.lineno)
                    scope_stack.append(node.name)
                    self.generic_visit(node)
                    scope_stack.pop()

                def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
                    add_sym(node.name, "function", node.lineno)
                    scope_stack.append(node.name)
                    self.generic_visit(node)
                    scope_stack.pop()

                def visit_ClassDef(self, node: ast.ClassDef) -> Any:
                    add_sym(node.name, "class", node.lineno)
                    scope_stack.append(node.name)
                    self.generic_visit(node)
                    scope_stack.pop()

                def visit_Assign(self, node: ast.Assign) -> Any:
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            add_sym(t.id, "var", node.lineno)
                    self.generic_visit(node)

            V().visit(tree)

        return {"symbols": [s.as_dict() for s in symbols]}

    def build_import_graph(self, files: List[FileRec]) -> Dict[str, Any]:
        # Map files → module names (best-effort; mirrors original path→module logic)
        file_to_mod: Dict[str, str] = {}
        nodes: Set[Tuple[str, str]] = set()
        for fr in files:
            if not self._is_python(fr.path):
                continue
            mod = fr.path[:-3].replace("/", ".")
            file_to_mod[fr.path] = mod
            nodes.add((mod, fr.path))

        edges: List[Dict[str, str]] = []
        for fr in files:
            if not self._is_python(fr.path):
                continue
            src = self._safe_text(fr) or ""
            try:
                tree = ast.parse(src)
            except Exception:
                continue

            this_mod = file_to_mod.get(fr.path, fr.path)
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        edges.append({"src_module": this_mod, "dst_module": a.name, "kind": "import"})
                elif isinstance(n, ast.ImportFrom):
                    mod = n.module or ""
                    if n.level and this_mod:
                        parts = this_mod.split(".")
                        if n.level <= len(parts):
                            mod = ".".join(parts[:-n.level] + ([mod] if mod else []))
                    edges.append({"src_module": this_mod, "dst_module": mod, "kind": "from"})

        return {
            "nodes": [{"module": m, "file_path": p} for (m, p) in sorted(nodes)],
            "edges": edges,
        }

    def build_call_graph(self, files: List[FileRec], symbols: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build a simple intra-file call graph:
          nodes: functions (qualname=n.name to match original)
          edges: caller → callee (by simple name resolution only)
        """
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, str]] = []

        for fr in files:
            if not self._is_python(fr.path):
                continue
            src = self._safe_text(fr) or ""
            try:
                tree = ast.parse(src)
            except Exception:
                continue

            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nodes.append({"qualname": n.name, "file_path": fr.path, "start_line": n.lineno})
                    # walk inside this function to find calls
                    for sub in ast.walk(n):
                        if isinstance(sub, ast.Call):
                            callee = None
                            if isinstance(sub.func, ast.Name):
                                callee = sub.func.id
                            elif isinstance(sub.func, ast.Attribute):
                                callee = sub.func.attr
                            if callee:
                                edges.append({"caller": n.name, "callee": callee, "via": "name"})

        return {"nodes": nodes, "edges": edges}

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


# --- legacy function shims (keep existing callers working) ---

_GRAPHS = Graphs()

def build_symbol_table(files: List[FileRec]) -> Dict[str, Any]:
    return _GRAPHS.build_symbol_table(files)

def build_import_graph(files: List[FileRec]) -> Dict[str, Any]:
    return _GRAPHS.build_import_graph(files)

def build_call_graph(files: List[FileRec], symbols: Dict[str, Any]) -> Dict[str, Any]:
    return _GRAPHS.build_call_graph(files, symbols)
