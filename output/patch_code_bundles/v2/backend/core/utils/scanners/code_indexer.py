#!/usr/bin/env python3
# code_indexer_standalone.py — lightweight multi-language code indexer (no CLI)
# Scans PROJECT_ROOT for .py, .sql, .sh; builds a compact index and writes all chunks into ONE file.

from __future__ import annotations
import ast
import json
import os
import sys
import re
import hashlib
import time
import platform
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union, Iterable, Set

# ======================= USER SETTINGS =======================

# Scan this repo (escaped backslashes; not a raw string)
PROJECT_ROOT = Path("/\\")
# Where to write outputs (single chunks file + full JSON)
OUTPUT_DIR = Path("/tests_adhoc")

# Max characters per printed/saved chunk marker block
MAX_CHARS = 18000

# ======================= CONFIG ==============================

DEFAULT_EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".venv", "venv", "env", "node_modules", "dist", "build", "site-packages",
    ".tox", ".idea", ".vscode"
}
DEFAULT_EXCLUDE_FILES_SUFFIX = {".pyc", ".pyo"}
SCHEMA_VERSION = "codeindex.v1"

# ======================= HELPERS =============================

def sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256(); h.update(b); return h.hexdigest()

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def rel_module_name(root: Path, file_path: Path) -> str:
    rel = file_path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    return ".".join(parts)

def first_line(s: Optional[str], max_len: int = 200) -> Optional[str]:
    if not s:
        return None
    return s.strip().splitlines()[0][:max_len]

def get_attr_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
    return None

def fmt_signature(fn: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> str:
    a = fn.args
    posonly = [x.arg for x in getattr(a, "posonlyargs", [])]
    args = [x.arg for x in a.args]
    vararg = ["*"+a.vararg.arg] if a.vararg else []
    kwonly = [x.arg for x in a.kwonlyargs]
    kwvar = ["**"+a.kwarg.arg] if a.kwarg else []
    items: List[str] = []
    if posonly:
        items += posonly + ["/"]
    items += args
    items += vararg
    if kwonly:
        if not vararg and "*" not in items:
            items += ["*"]
        items += kwonly
    items += kwvar
    return "(" + ", ".join(items) + ")"

def count_loc(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0

def scan_files(root: Path, exts: Iterable[str]) -> List[Path]:
    exts = set(exts)
    paths: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDE_DIRS and not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if any(fn.endswith(sfx) for sfx in DEFAULT_EXCLUDE_FILES_SUFFIX):
                continue
            if p.suffix.lower() in exts:
                paths.append(p)
    return paths

# ======================= PYTHON DATA MODEL ===================

@dataclass
class FileRec:
    path: str
    hash: str
    loc: int

@dataclass
class ImportRec:
    alias: str
    target: str

@dataclass
class FuncRec:
    sym: str
    name: str
    signature: str
    lineno: int
    end_lineno: Optional[int]
    decorators: List[str]
    doc: Optional[str]

@dataclass
class ClassRec:
    sym: str
    name: str
    bases: List[str]
    lineno: int
    end_lineno: Optional[int]
    doc: Optional[str]
    methods: Dict[str, FuncRec]

@dataclass
class CallEdge:
    src: str
    dst: str
    confidence: str  # "high" | "medium" | "low"
    lineno: int

@dataclass
class ModuleIndex:
    module: str
    path: str
    imports: List[ImportRec]
    functions: Dict[str, FuncRec]
    classes: Dict[str, ClassRec]
    calls: List[CallEdge]

# ======================= PYTHON VISITOR ======================

class PyModuleVisitor(ast.NodeVisitor):
    def __init__(self, module: str, path: Path):
        super().__init__()
        self.module = module
        self.path = path
        self.imports: Dict[str, str] = {}
        self.functions: Dict[str, FuncRec] = {}
        self.classes: Dict[str, ClassRec] = {}
        self.calls: List[CallEdge] = []
        self._class_stack: List[str] = []
        self._func_stack: List[str] = []
        self.local_defs: Set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            target = alias.name
            local = alias.asname or alias.name.split(".")[0]
            self.imports[local] = target

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = ("." * node.level + (node.module or "")).strip(".")
        for alias in node.names:
            target = f"{base}.{alias.name}".strip(".")
            local = alias.asname or alias.name
            self.imports[local] = target

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        cls_sym = f"{self.module}:{node.name}"
        bases = []
        for b in node.bases:
            nm = get_attr_name(b)
            if nm:
                bases.append(nm)
        rec = ClassRec(
            sym=cls_sym,
            name=node.name,
            bases=bases,
            lineno=node.lineno,
            end_lineno=getattr(node, "end_lineno", None),
            doc=first_line(ast.get_docstring(node)),
            methods={},
        )
        self.classes[cls_sym] = rec
        self.local_defs.add(node.name)
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def _make_func(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef], sym: str) -> FuncRec:
        decorators = []
        for d in node.decorator_list:
            nm = get_attr_name(d)
            if nm:
                decorators.append(nm)
        return FuncRec(
            sym=sym,
            name=node.name,
            signature=fmt_signature(node),
            lineno=node.lineno,
            end_lineno=getattr(node, "end_lineno", None),
            decorators=decorators,
            doc=first_line(ast.get_docstring(node)),
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_function_like(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_function_like(node)

    def _handle_function_like(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> None:
        if self._class_stack:
            cls = self._class_stack[-1]
            sym = f"{self.module}:{cls}.{node.name}"
            rec = self._make_func(node, sym)
            cls_sym = f"{self.module}:{cls}"
            self.classes[cls_sym].methods[sym] = rec
            self.local_defs.add(node.name)
        else:
            sym = f"{self.module}:{node.name}"
            rec = self._make_func(node, sym)
            self.functions[sym] = rec
            self.local_defs.add(node.name)

        self._func_stack.append(sym)
        self.generic_visit(node)
        self._func_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        src = self._func_stack[-1] if self._func_stack else self.module
        target_name = self._resolve_call_target(node.func)
        self.calls.append(CallEdge(
            src=src,
            dst=target_name.name,
            confidence=target_name.confidence,
            lineno=node.lineno,
        ))
        self.generic_visit(node)

    def _resolve_call_target(self, func_node: ast.AST):
        class Resolved:
            def __init__(self, name: str, confidence: str):
                self.name = name; self.confidence = confidence

        if isinstance(func_node, ast.Attribute):
            dotted = get_attr_name(func_node)
            if dotted:
                root = dotted.split(".", 1)[0]
                if root == "self" and self._class_stack:
                    method = dotted.split(".", 1)[1] if "." in dotted else ""
                    cls = self._class_stack[-1]
                    if method:
                        return Resolved(f"{self.module}:{cls}.{method}", "high")
                if root in self.imports:
                    imp = self.imports[root]
                    remainder = dotted[len(root):]
                    name = (imp + remainder) if remainder else imp
                    return Resolved(name, "medium")
                return Resolved(dotted, "low")

        if isinstance(func_node, ast.Name):
            nm = func_node.id
            if nm in self.local_defs:
                return Resolved(f"{self.module}:{nm}", "medium")
            if nm in self.imports:
                return Resolved(self.imports[nm], "medium")
            return Resolved(nm, "low")

        return Resolved("<unknown>", "low")

# ======================= SQL PARSER (HEURISTIC) ==============

_SQL_DEF_RE = re.compile(
    r"""^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(TABLE|VIEW|FUNCTION|PROCEDURE|TRIGGER|INDEX)\s+([^\s(]+)""",
    re.IGNORECASE | re.VERBOSE
)
_SQL_EXEC_RE = re.compile(r'\b(?:EXEC|EXECUTE|CALL)\s+([^\s(;,]+)', re.IGNORECASE)
_SQL_FROM_RE = re.compile(r'\b(?:FROM|JOIN|UPDATE|INTO|DELETE\s+FROM|MERGE\s+INTO)\s+([^\s,;()]+)', re.IGNORECASE)

def _strip_sql_comments(text: str) -> str:
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r'--.*?$', '', text, flags=re.MULTILINE)
    return text

def _canon_sql_name(raw: str) -> str:
    parts = re.split(r'\.', raw.strip())
    cleaned = []
    for p in parts:
        p = p.strip()
        if p.startswith('[') and p.endswith(']'):
            p = p[1:-1]
        elif p.startswith('"') and p.endswith('"'):
            p = p[1:-1]
        elif p.startswith('`') and p.endswith('`'):
            p = p[1:-1]
        cleaned.append(p)
    return ".".join(cleaned)

@dataclass
class SqlDef:
    kind: str   # TABLE/VIEW/FUNCTION/PROCEDURE/TRIGGER/INDEX
    name: str
    lineno: int

@dataclass
class SqlRef:
    kind: str   # table_or_view/proc/function
    name: str
    lineno: int

@dataclass
class SqlEdge:
    src: str    # file path or def name
    dst: str    # referenced object
    kind: str   # 'exec' | 'uses-table-or-view'
    lineno: int

@dataclass
class SqlFileIndex:
    path: str
    defs: List[SqlDef]
    refs: List[SqlRef]
    edges: List[SqlEdge]

def index_sql_file(root: Path, file_path: Path) -> Optional[SqlFileIndex]:
    try:
        raw = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    text = _strip_sql_comments(raw)
    defs: List[SqlDef] = []
    refs: List[SqlRef] = []
    edges: List[SqlEdge] = []
    rel = str(file_path.relative_to(root))

    lines = text.splitlines()
    for i, line in enumerate(lines, start=1):
        m = _SQL_DEF_RE.match(line)
        if m:
            kind = m.group(1).upper()
            name = _canon_sql_name(m.group(2))
            defs.append(SqlDef(kind=kind, name=name, lineno=i))
        for m in _SQL_EXEC_RE.finditer(line):
            name = _canon_sql_name(m.group(1))
            refs.append(SqlRef(kind="proc", name=name, lineno=i))
            edges.append(SqlEdge(src=rel, dst=name, kind="exec", lineno=i))
        for m in _SQL_FROM_RE.finditer(line):
            rawname = m.group(1).rstrip(';')
            name = _canon_sql_name(rawname)
            if '(' in name or name.upper().startswith('SELECT'):
                continue
            refs.append(SqlRef(kind="table_or_view", name=name, lineno=i))
            edges.append(SqlEdge(src=rel, dst=name, kind="uses-table-or-view", lineno=i))

    return SqlFileIndex(path=rel, defs=defs, refs=refs, edges=edges)

# ======================= SHELL PARSER (HEURISTIC) ============

_SHEBANG_RE = re.compile(r'^\s*#!\s*(\S+)')
_SH_FUNC_RE1 = re.compile(r'^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{')
_SH_FUNC_RE2 = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)\s*\{')
_SH_SOURCE_RE = re.compile(r'^\s*(?:source|\.)\s+([^\s#;]+)')
_SH_EXPORT_RE = re.compile(r'^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)')
_SH_ASSIGN_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)=')
_SH_SUBSHELL_RE = re.compile(r'\$\(([^)]*)\)|`([^`]*)`')

_SH_KEYWORDS = {
    "if","then","fi","elif","else","for","in","do","done","while","until","case","esac",
    "select","time","coproc","function","local","export","declare","typeset","readonly",
    "eval","exec","exit","return","shift","getopts","test","[","]","[[","]]","source",".",
    "{","}","(",")","!","||","&&","true","false","cd","umask","ulimit","pwd","set","unset","trap","alias","unalias"
}

@dataclass
class ShFunction:
    name: str
    lineno: int
    end_lineno: Optional[int]

@dataclass
class ShEdge:
    src: str    # function name or file path
    dst: str    # command, function, or sourced file
    kind: str   # 'calls-external' | 'calls-function' | 'sources'
    lineno: int

@dataclass
class ShFileIndex:
    path: str
    interpreter: Optional[str]
    functions: List[ShFunction]
    commands: List[str]
    sources: List[str]
    env_exports: List[str]
    edges: List[ShEdge]

def _tokenize_sh_commands(line: str) -> List[str]:
    line = re.sub(r'#.*$', '', line).strip()
    if not line:
        return []
    parts = re.split(r'[|;]|&&|\|\|', line)
    tokens: List[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if _SH_ASSIGN_RE.match(part):
            continue
        m = re.match(r'^[A-Za-z0-9_./-]+', part)
        if m:
            tokens.append(m.group(0))
    return tokens

def index_sh_file(root: Path, file_path: Path) -> Optional[ShFileIndex]:
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    rel = str(file_path.relative_to(root))
    interpreter = None
    functions: List[ShFunction] = []
    commands_set: Set[str] = set()
    sources: Set[str] = set()
    env_exports: Set[str] = set()
    edges: List[ShEdge] = []

    lines = text.splitlines()
    if lines:
        m = _SHEBANG_RE.match(lines[0])
        if m:
            interpreter = m.group(1)

    func_names: Set[str] = set()
    for i, line in enumerate(lines, start=1):
        m1 = _SH_FUNC_RE1.match(line)
        m2 = _SH_FUNC_RE2.match(line)
        if m1:
            name = m1.group(1); func_names.add(name); functions.append(ShFunction(name=name, lineno=i, end_lineno=None))
        elif m2:
            name = m2.group(1); func_names.add(name); functions.append(ShFunction(name=name, lineno=i, end_lineno=None))

    current_func = None
    func_iter = iter(sorted(functions, key=lambda f: f.lineno))
    next_func = next(func_iter, None)

    for i, line in enumerate(lines, start=1):
        if next_func and i == next_func.lineno:
            current_func = next_func.name
            next_func = next(func_iter, None)

        ms = _SH_SOURCE_RE.match(line)
        if ms:
            src_file = ms.group(1)
            sources.add(src_file)
            edges.append(ShEdge(src=current_func or rel, dst=src_file, kind="sources", lineno=i))

        me = _SH_EXPORT_RE.match(line)
        if me:
            env_exports.add(me.group(1))

        for g1, g2 in _SH_SUBSHELL_RE.findall(line):
            inner = g1 or g2
            for tok in _tokenize_sh_commands(inner):
                if tok in _SH_KEYWORDS:
                    continue
                commands_set.add(tok)
                kind = "calls-function" if tok in func_names else "calls-external"
                edges.append(ShEdge(src=current_func or rel, dst=tok, kind=kind, lineno=i))

        for tok in _tokenize_sh_commands(line):
            if tok in _SH_KEYWORDS:
                continue
            if tok.startswith("$"):
                continue
            commands_set.add(tok)
            kind = "calls-function" if tok in func_names else "calls-external"
            edges.append(ShEdge(src=current_func or rel, dst=tok, kind=kind, lineno=i))

    return ShFileIndex(
        path=rel,
        interpreter=interpreter,
        functions=functions,
        commands=sorted(commands_set),
        sources=sorted(sources),
        env_exports=sorted(env_exports),
        edges=edges
    )

# ======================= BUILD INDEX =========================

def index_python_file(root: Path, file_path: Path) -> Optional[ModuleIndex]:
    try:
        src = file_path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(src, filename=str(file_path))
    except Exception:
        return None

    module = rel_module_name(root, file_path)
    visitor = PyModuleVisitor(module, file_path)
    visitor.visit(tree)

    imports = [ImportRec(alias=k, target=v) for k, v in sorted(visitor.imports.items())]
    return ModuleIndex(
        module=module,
        path=str(file_path.relative_to(root)),
        imports=imports,
        functions=visitor.functions,
        classes=visitor.classes,
        calls=visitor.calls,
    )

def build_index(root: Path) -> Dict:
    root = root.resolve()

    all_exts = {".py", ".sql", ".sh"}
    all_files: List[Path] = scan_files(root, all_exts)
    files_meta: List[FileRec] = [
        FileRec(path=str(p.relative_to(root)), hash=sha256_file(p), loc=count_loc(p))
        for p in all_files
    ]

    py_files = [p for p in all_files if p.suffix.lower() == ".py"]
    py_modules: List[ModuleIndex] = []
    for p in py_files:
        mi = index_python_file(root, p)
        if mi:
            py_modules.append(mi)

    sql_files = [p for p in all_files if p.suffix.lower() == ".sql"]
    sql_units: List[SqlFileIndex] = []
    for p in sql_files:
        si = index_sql_file(root, p)
        if si:
            sql_units.append(si)

    sh_files = [p for p in all_files if p.suffix.lower() == ".sh"]
    sh_units: List[ShFileIndex] = []
    for p in sh_files:
        shi = index_sh_file(root, p)
        if shi:
            sh_units.append(shi)

    py_out = []
    for m in py_modules:
        py_out.append({
            "module": m.module,
            "path": m.path,
            "imports": [asdict(x) for x in m.imports],
            "functions": {k: asdict(v) for k, v in m.functions.items()},
            "classes": {
                k: {
                    **{kk: vv for kk, vv in asdict(v).items() if kk != "methods"},
                    "methods": {mk: asdict(mv) for mk, mv in v.methods.items()},
                } for k, v in m.classes.items()
            },
            "calls": [asdict(c) for c in m.calls],
        })

    sql_out = [asdict(u) for u in sql_units]
    sh_out = [asdict(u) for u in sh_units]

    index = {
        "schema": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": platform.python_version(),
        "root": str(root),
        "files": [asdict(f) for f in files_meta],
        "python_modules": py_out,
        "sql_units": sql_out,
        "sh_units": sh_out,
        "stats": {
            "n_files": len(files_meta),
            "n_py_modules": len(py_out),
            "n_sql_files": len(sql_out),
            "n_sh_files": len(sh_out),
            "n_py_functions": sum(len(m["functions"]) for m in py_out),
            "n_py_classes": sum(len(m["classes"]) for m in py_out),
            "n_py_calls": sum(len(m["calls"]) for m in py_out),
            "n_sql_defs": sum(len(u["defs"]) for u in sql_out),
            "n_sql_refs": sum(len(u["refs"]) for u in sql_out),
            "n_sh_functions": sum(len(u["functions"]) for u in sh_out),
            "n_sh_commands": sum(len(u["commands"]) for u in sh_out),
        },
    }
    payload = json.dumps(index, separators=(",", ":"), ensure_ascii=False)
    index["content_hash"] = sha256_bytes(payload.encode("utf-8"))
    return index

# ======================= WRITE SINGLE FILE ===================

def write_chunks_single_file(index: Dict, max_chars: int, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    s = json.dumps(index, separators=(",", ":"), ensure_ascii=False)
    n = (len(s) + max_chars - 1) // max_chars or 1
    ts = time.strftime("%Y%m%d_%H%M%S")
    base = f"code_index_{index['content_hash'][:12]}_{ts}"

    # Save full JSON once for reference
    full_path = out_dir / f"{base}_full.json"
    with full_path.open("w", encoding="utf-8") as f:
        f.write(s)

    # Save ALL chunks into a single .txt with markers
    chunks_path = out_dir / f"{base}_chunks.txt"
    with chunks_path.open("w", encoding="utf-8") as f:
        if n == 1:
            f.write("==== CODE_INDEX CHUNK 1/1 ====\n")
            f.write(s + "\n")
            f.write("==== CODE_INDEX END ====\n")
        else:
            for i in range(n):
                a = i * max_chars
                b = min(len(s), a + max_chars)
                f.write(f"==== CODE_INDEX CHUNK {i+1}/{n} ====\n")
                f.write(s[a:b] + "\n")
            f.write("==== CODE_INDEX END ====\n")
    return chunks_path

# ======================= MAIN (NO CLI) =======================

def run() -> None:
    root = PROJECT_ROOT
    if not root.exists():
        print(f"ERROR: PROJECT_ROOT not found: {root}", file=sys.stderr)
        return

    index = build_index(root)
    st = index["stats"]
    chunks_file = write_chunks_single_file(index, max_chars=MAX_CHARS, out_dir=OUTPUT_DIR)

    print(
        f"[code_indexer] root='{root}' files={st['n_files']} py_modules={st['n_py_modules']} "
        f"sql_files={st['n_sql_files']} sh_files={st['n_sh_files']} "
        f"py_funcs={st['n_py_functions']} py_classes={st['n_py_classes']} py_calls={st['n_py_calls']} "
        f"sql_defs={st['n_sql_defs']} sh_funcs={st['n_sh_functions']} "
        f"hash={index['content_hash']} chunks_file='{chunks_file}'",
        file=sys.stderr
    )

if __name__ == "__main__":
    run()
