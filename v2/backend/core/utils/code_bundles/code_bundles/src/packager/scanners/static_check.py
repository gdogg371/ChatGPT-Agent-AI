from __future__ import annotations

import ast
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

__all__ = ["scan"]


# ---------------------------
# Data structures / reporting
# ---------------------------

@dataclass
class Issue:
    file: str
    line: int
    col: int
    check: str           # "syntax-error" | "import-unresolved" | "call-signature" | "name-unresolved" | "io-error"
    severity: str        # "error" | "warning" | "info"
    code: str            # "E100" | "W200" | "E210" | "E211" | "E212" | "E213" | "W300" | "E090"
    message: str
    symbol: Optional[str] = None   # function/name if relevant
    target: Optional[str] = None   # module.func if relevant
    details: Dict[str, object] = field(default_factory=dict)

    def to_json(self) -> Dict[str, object]:
        out = {
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "check": self.check,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.symbol is not None:
            out["symbol"] = self.symbol
        if self.target is not None:
            out["target"] = self.target
        if self.details:
            out["details"] = self.details
        return out


@dataclass
class FuncSig:
    module: str
    name: str
    posonly: List[str]
    args: List[str]
    vararg: Optional[str]
    kwonly: List[str]
    varkw: Optional[str]
    required_positional: int       # count of required positional params (no defaults)
    keyword_names: Set[str]        # args + kwonly (posonly are not keyword-eligible)

    def accepts_varargs(self) -> bool:
        return self.vararg is not None

    def accepts_varkw(self) -> bool:
        return self.varkw is not None

    def arity_total_positional(self) -> int:
        return len(self.posonly) + len(self.args)

    def full_qualname(self) -> str:
        return f"{self.module}.{self.name}" if self.module else self.name


@dataclass
class ModuleInfo:
    path: Path
    module: str
    tree: Optional[ast.AST] = None
    functions: Dict[str, FuncSig] = field(default_factory=dict)
    names_defined: Set[str] = field(default_factory=set)  # module-level bindings
    # alias -> (module, attr)  e.g., "scan_entrypoints": ("pack.scanners.entrypoints", "scan")
    # alias -> (module, None)  for "import pack.scanners.entrypoints as entrypoints"
    imports: Dict[str, Tuple[str, Optional[str]]] = field(default_factory=dict)


# ---------------------------
# Project indexing / parsing
# ---------------------------

class ProjectIndex:
    def __init__(self, root: Path, exclude: Optional[List[str]] = None):
        self.root = Path(root).resolve()
        self.exclude = exclude or []
        self.modules_by_path: Dict[Path, ModuleInfo] = {}
        self.modules_by_name: Dict[str, ModuleInfo] = {}
        self.issues: List[Issue] = []

    # --- file discovery ---

    def _should_skip(self, path: Path) -> bool:
        rel = str(path.resolve().relative_to(self.root)).replace("\\", "/")
        return any(fnmatch.fnmatch(rel, pat) for pat in self.exclude)

    def walk_py_files(self) -> List[Path]:
        out = []
        for p in self.root.rglob("*.py"):
            if self._should_skip(p):
                continue
            out.append(p.resolve())
        return out

    # --- module mapping ---

    def module_name_for(self, path: Path) -> str:
        rel = path.resolve().relative_to(self.root)
        parts = list(rel.parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1][:-3]
        return ".".join(parts)

    # --- parsing ---

    def parse_file(self, path: Path) -> Optional[ast.AST]:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            self.issues.append(Issue(
                file=str(path), line=1, col=0,
                check="io-error", severity="error", code="E090",
                message=f"Failed to read file: {e}"
            ))
            return None
        try:
            return ast.parse(text, filename=str(path))
        except SyntaxError as e:
            self.issues.append(Issue(
                file=str(path),
                line=getattr(e, "lineno", 1) or 1,
                col=getattr(e, "offset", 0) or 0,
                check="syntax-error",
                severity="error",
                code="E100",
                message=f"SyntaxError: {e.msg}",
            ))
            return None

    def register_tree(self, path: Path, tree: ast.AST):
        # Parent links for quick checks
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                setattr(child, "parent", parent)

        mi = ModuleInfo(path=path, module=self.module_name_for(path), tree=tree)
        self._index_imports(tree, mi)
        self._index_functions(tree, mi)
        self.modules_by_path[path] = mi
        self.modules_by_name[mi.module] = mi

    # --- indexing helpers ---

    def _index_functions(self, tree: ast.AST, mi: ModuleInfo):
        for node in tree.body if isinstance(tree, ast.Module) else []:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sig = self._build_sig(mi.module, node)
                mi.functions[node.name] = sig
                mi.names_defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    for name in _iter_assigned_names(tgt):
                        mi.names_defined.add(name)
            elif isinstance(node, ast.AnnAssign):
                for name in _iter_assigned_names(node.target):
                    mi.names_defined.add(name)

    def _build_sig(self, module: str, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> FuncSig:
        args = fn.args
        posonly = [a.arg for a in getattr(args, "posonlyargs", [])]
        normargs = [a.arg for a in args.args]
        vararg = args.vararg.arg if args.vararg else None
        kwonly = [a.arg for a in args.kwonlyargs]
        varkw = args.kwarg.arg if args.kwarg else None

        total_pos = len(posonly) + len(normargs)
        num_defaults = len(args.defaults)
        required_pos = max(total_pos - num_defaults, 0)

        keyword_names = set(normargs + kwonly)  # posonly not valid as keyword names

        return FuncSig(
            module=module, name=fn.name,
            posonly=posonly, args=normargs, vararg=vararg,
            kwonly=kwonly, varkw=varkw,
            required_positional=required_pos,
            keyword_names=keyword_names
        )

    def _index_imports(self, tree: ast.AST, mi: ModuleInfo):
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name
                    asname = alias.asname or mod.split(".")[-1]
                    mi.imports[asname] = (mod, None)
                    mi.names_defined.add(asname)
            elif isinstance(node, ast.ImportFrom):
                level = node.level or 0
                modname = node.module or ""
                base_module = self._resolve_relative_module(mi.module, modname, level)
                if base_module is None:
                    self.issues.append(Issue(
                        file=str(mi.path), line=node.lineno, col=node.col_offset,
                        check="import-unresolved", severity="warning", code="W200",
                        message=f"Cannot resolve relative import: from {'.'*level}{modname} import ...",
                        symbol=modname
                    ))
                    continue
                for alias in node.names:
                    asname = alias.asname or alias.name
                    mi.imports[asname] = (base_module, alias.name)
                    mi.names_defined.add(asname)

    def _resolve_relative_module(self, current_module: str, name: str, level: int) -> Optional[str]:
        if level == 0:
            return name or ""
        parts = current_module.split(".")
        if level > len(parts):
            return None
        base = parts[: len(parts) - level]
        if name:
            base.extend(name.split("."))
        return ".".join(base) if base else None

    # -----------------
    # Analysis routines
    # -----------------

    def analyze(self):
        for mi in self.modules_by_name.values():
            if mi.tree is None:
                continue
            self._check_unresolved_names(mi, mi.tree)
            self._check_calls(mi, mi.tree)

    def _check_unresolved_names(self, mi: ModuleInfo, tree: ast.AST):
        module_bound = set(mi.names_defined) | set(mi.functions.keys())
        builtin_names = set(dir(__import__("builtins")))

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local_bound = set(a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs)
                if node.args.vararg:
                    local_bound.add(node.args.vararg.arg)
                if node.args.kwarg:
                    local_bound.add(node.args.kwarg.arg)
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Assign):
                        for tgt in sub.targets:
                            for nm in _iter_assigned_names(tgt):
                                local_bound.add(nm)
                    elif isinstance(sub, ast.AnnAssign):
                        for nm in _iter_assigned_names(sub.target):
                            local_bound.add(nm)
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                        nm = sub.id
                        if nm in local_bound or nm in module_bound or nm in builtin_names:
                            continue
                        self.issues.append(Issue(
                            file=str(mi.path), line=sub.lineno, col=sub.col_offset,
                            check="name-unresolved", severity="warning", code="W300",
                            message=f"Unresolved name: {nm}", symbol=nm
                        ))
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                nm = node.id
                if nm in module_bound or nm in builtin_names:
                    continue
                self.issues.append(Issue(
                    file=str(mi.path), line=node.lineno, col=node.col_offset,
                    check="name-unresolved", severity="warning", code="W300",
                    message=f"Unresolved name: {nm}", symbol=nm
                ))

    def _check_calls(self, mi: ModuleInfo, tree: ast.AST):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Skip dynamic callsites we can't reason about statically
            if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                continue
            sig = self._resolve_call_target(mi, node.func)
            if not sig:
                continue  # only check resolvable project-local functions

            pos_count = len(node.args)
            kw_names = [k.arg for k in node.keywords if k.arg is not None]

            dup_kw = _find_duplicates(kw_names)
            if dup_kw:
                self.issues.append(Issue(
                    file=str(mi.path), line=node.lineno, col=node.col_offset,
                    check="call-signature", severity="error", code="E210",
                    message=f"Duplicate keyword(s): {', '.join(sorted(dup_kw))}",
                    symbol=_call_symbol(node.func),
                    target=sig.full_qualname(),
                    details={"keywords": kw_names},
                ))

            max_pos = sig.arity_total_positional()
            if not sig.accepts_varargs() and pos_count > max_pos:
                self.issues.append(Issue(
                    file=str(mi.path), line=node.lineno, col=node.col_offset,
                    check="call-signature", severity="error", code="E211",
                    message=f"Too many positional arguments: got {pos_count}, allow {max_pos}",
                    symbol=_call_symbol(node.func),
                    target=sig.full_qualname(),
                    details={"positional_given": pos_count, "positional_allowed": max_pos},
                ))

            unexpected = [k for k in kw_names if k not in sig.keyword_names]
            if unexpected and not sig.accepts_varkw():
                self.issues.append(Issue(
                    file=str(mi.path), line=node.lineno, col=node.col_offset,
                    check="call-signature", severity="error", code="E212",
                    message=f"Unexpected keyword argument(s): {', '.join(sorted(unexpected))}",
                    symbol=_call_symbol(node.func),
                    target=sig.full_qualname(),
                    details={"unexpected": unexpected},
                ))

            req = list(sig.posonly + sig.args)[: sig.required_positional]
            filled = set()
            # positional map to front of req
            for i in range(min(pos_count, len(req))):
                filled.add(req[i])
            # keywords fill by name
            for k in kw_names:
                if k in req:
                    filled.add(k)
            missing = [n for n in req if n not in filled]
            if missing:
                self.issues.append(Issue(
                    file=str(mi.path), line=node.lineno, col=node.col_offset,
                    check="call-signature", severity="error", code="E213",
                    message=f"Missing required argument(s): {', '.join(missing)}",
                    symbol=_call_symbol(node.func),
                    target=sig.full_qualname(),
                    details={"missing": missing},
                ))

    # -------------------------
    # Resolution & lookup logic
    # -------------------------

    def _resolve_call_target(self, mi: ModuleInfo, func: ast.AST) -> Optional[FuncSig]:
        # Name()
        if isinstance(func, ast.Name):
            name = func.id
            # local function?
            if name in mi.functions:
                return mi.functions[name]
            # imported symbol?
            if name in mi.imports:
                mod, attr = mi.imports[name]
                if attr is None:
                    return None
                target_mod = self.modules_by_name.get(mod)
                if target_mod and attr in target_mod.functions:
                    return target_mod.functions[attr]
                return None
            return None

        # Attribute(): M.f where M is imported module alias
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            base = func.value.id
            attr = func.attr
            if base in mi.imports:
                mod, imported_attr = mi.imports[base]
                if imported_attr is None:
                    target_mod = self.modules_by_name.get(mod)
                    if target_mod and attr in target_mod.functions:
                        return target_mod.functions[attr]
        return None


# ---------------------------
# Utility helpers
# ---------------------------

def _iter_assigned_names(node: ast.AST):
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            yield from _iter_assigned_names(elt)


def _find_duplicates(items: List[str]) -> Set[str]:
    seen: Set[str] = set()
    dup: Set[str] = set()
    for x in items:
        if x in seen:
            dup.add(x)
        else:
            seen.add(x)
    return dup


def _call_symbol(func: ast.AST) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


# ---------------------------
# Public entry for manifest
# ---------------------------

def static_check_scan(project_root: Path, exclude: Optional[List[str]] = None) -> List[dict]:
    """
    Static checks for a project. Returns a list[dict] issues suitable for manifest consumption.
    - Syntax errors
    - Unresolved relative imports
    - Unresolved names (best-effort lexical)
    - Function call signature validation for resolvable in-project targets
    """
    idx = ProjectIndex(project_root, exclude=exclude)
    files = idx.walk_py_files()
    for p in files:
        tree = idx.parse_file(p)
        if tree is None:
            continue
        idx.register_tree(p, tree)
    idx.analyze()
    return [i.to_json() for i in idx.issues]

