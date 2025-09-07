# File: v2/backend/core/utils/code_bundles/code_bundles/env_index.py
"""
Environment variable scanner (stdlib-only).

Emits JSONL-ready records:

Per .env-like file (templates/samples included):
  {
    "kind": "env.file",
    "path": "path/.env.example",
    "vars": ["DATABASE_URL","OPENAI_API_KEY", ...],
    "count": 2,
    "is_template": true
  }

Per config file with placeholders (${VAR}, $VAR):
  {
    "kind": "env.required",
    "path": "config/app.yml",
    "vars": ["DATABASE_URL","REDIS_URL"],
    "count": 2
  }

Per code usage (Python):
  {
    "kind": "env.usage",
    "language": "python",
    "path": "v2/backend/.../settings.py",
    "vars": ["DATABASE_URL","OPENAI_API_KEY"],
    "count": 2,
    "calls": {"getenv": 3, "environ_index": 1, "environ_get": 0}
  }

Per code usage (JS/TS):
  {
    "kind": "env.usage",
    "language": "javascript",
    "path": "web/src/app.ts",
    "vars": ["NODE_ENV","API_URL"],
    "count": 2,
    "calls": {"process_env": 4}
  }

Aggregate summary:
  {
    "kind": "env.summary",
    "files_scanned": 123,
    "sources": {
      "env_files": 3,
      "configs": 5,
      "python": 47,
      "javascript": 12
    },
    "unique_vars": {
      "env_files": [...],
      "configs": [...],
      "python":  [...],
      "javascript": [...],
      "all":     [...]
    },
    "counts": {
      "env_files": 10,
      "configs": 8,
      "python": 14,
      "javascript": 6,
      "all": 22
    }
  }

Notes
-----
* Paths are repo-relative POSIX. The caller (runner) should map them to
  local vs GitHub path modes before appending to the manifest.
* Parsing is intentionally conservative and dependency-free.
"""

from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

try:  # Python 3.11+
    import tomllib  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

RepoItem = Tuple[Path, str]  # (local_path, repo_relative_posix)

# Limit reads to avoid slurping huge files
_MAX_READ_BYTES = 2 * 1024 * 1024  # 2 MiB


# ──────────────────────────────────────────────────────────────────────────────
# Generic helpers
# ──────────────────────────────────────────────────────────────────────────────

def _read_text_limited(p: Path, limit: int = _MAX_READ_BYTES) -> str:
    try:
        with p.open("rb") as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            data = data[:limit]
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""

def _strip_inline_comment(line: str) -> str:
    s = line.rstrip("\n")
    # Full-line comment
    if s.strip().startswith("#"):
        return ""
    # Remove inline comments that start with ' #' or unescaped '#'
    pos = s.find(" #")
    if pos != -1:
        return s[:pos].rstrip()
    # Best effort: if there's a '#' not inside quotes, strip from there
    if "#" in s:
        # crude: if both quotes are balanced before '#', keep. else strip.
        before, _hash, _after = s.partition("#")
        # If the number of quotes in 'before' is even, treat as not in quotes
        if before.count("'") % 2 == 0 and before.count('"') % 2 == 0:
            return before.rstrip()
    return s


# ──────────────────────────────────────────────────────────────────────────────
# .env-like files
# ──────────────────────────────────────────────────────────────────────────────

_ENV_FILE_BASENAMES = {
    ".env", ".env.local", ".env.development", ".env.production",
    ".env.test", ".env.ci", ".env.sample", ".env.example", ".env.template",
}
_ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template")

_ENV_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

def _is_env_like(basename: str) -> bool:
    if basename in _ENV_FILE_BASENAMES:
        return True
    if basename.startswith(".env."):
        return True
    if basename.endswith(".env"):
        return True
    return False

def _is_template_env(basename: str) -> bool:
    if basename in (".env.example", ".env.sample", ".env.template"):
        return True
    for suf in _ENV_TEMPLATE_SUFFIXES:
        if basename.endswith(suf):
            return True
    return False

def _parse_env_vars(text: str) -> List[str]:
    vars_set: Set[str] = set()
    for raw in text.splitlines():
        line = _strip_inline_comment(raw)
        if not line.strip():
            continue
        m = _ENV_KEY_RE.match(line)
        if not m:
            continue
        key = m.group(1)
        if key:
            vars_set.add(key)
    return sorted(vars_set)


# ──────────────────────────────────────────────────────────────────────────────
# Config placeholders
# ──────────────────────────────────────────────────────────────────────────────

# ${VAR} or ${VAR:-default}
_RE_DOLLAR_BRACE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::-[^}]*)?\}")
# Bare $VAR (avoid $$ and $1; require uppercase alpha at start)
_RE_DOLLAR = re.compile(r"(?<![\w$])\$([A-Z][A-Z0-9_]*)\b")

_CONFIG_EXTS = {".yml", ".yaml", ".json", ".toml", ".ini", ".cfg", ".conf"}

def _extract_placeholders_from_text(text: str) -> Set[str]:
    out: Set[str] = set()
    for m in _RE_DOLLAR_BRACE.finditer(text):
        out.add(m.group(1))
    for m in _RE_DOLLAR.finditer(text):
        out.add(m.group(1))
    return out

def _extract_placeholders_from_json(text: str) -> Set[str]:
    out: Set[str] = set()
    try:
        data = json.loads(text)
    except Exception:
        return _extract_placeholders_from_text(text)

    def walk(x):
        if isinstance(x, str):
            out.update(_extract_placeholders_from_text(x))
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    return out

def _extract_placeholders_from_toml(text: str) -> Set[str]:
    if tomllib is None:
        return _extract_placeholders_from_text(text)
    out: Set[str] = set()
    try:
        data = tomllib.loads(text)
    except Exception:
        return _extract_placeholders_from_text(text)

    def walk(x):
        if isinstance(x, str):
            out.update(_extract_placeholders_from_text(x))
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Python env usage (AST)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _PyEnvUsage:
    vars: Set[str]
    calls_getenv: int = 0
    calls_environ_index: int = 0
    calls_environ_get: int = 0

class _PyEnvVisitor(ast.NodeVisitor):
    def __init__(self):
        self.os_aliases: Set[str] = set()       # names that refer to os module
        self.environ_aliases: Set[str] = set()  # names that refer to os.environ
        self.getenv_aliases: Set[str] = set()   # names that resolve to os.getenv
        self.usage = _PyEnvUsage(vars=set())

    # --- imports
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "os":
                self.os_aliases.add(alias.asname or "os")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "os":
            for alias in node.names:
                name = alias.name
                asname = alias.asname or alias.name
                if name == "environ":
                    self.environ_aliases.add(asname)
                elif name == "getenv":
                    self.getenv_aliases.add(asname)

    # --- helpers
    def _const_str(self, node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    def _is_os_environ_attr(self, node: ast.AST) -> bool:
        # Attribute(Name(os_alias), "environ")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.attr == "environ" and node.value.id in self.os_aliases:
                return True
        return False

    def _is_environ_name(self, node: ast.AST) -> bool:
        # Name in environ_aliases
        return isinstance(node, ast.Name) and node.id in self.environ_aliases

    def _is_os_getenv_attr(self, node: ast.AST) -> bool:
        # Attribute(Name(os_alias), "getenv")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.attr == "getenv" and node.value.id in self.os_aliases:
                return True
        return False

    # --- Subscript: os.environ["VAR"] or environ["VAR"]
    def visit_Subscript(self, node: ast.Subscript) -> None:
        value = node.value
        if self._is_os_environ_attr(value) or self._is_environ_name(value):
            # key could be in slice (different AST versions)
            key_node = getattr(node, "slice", None)
            if isinstance(key_node, ast.Index):  # py<3.9
                key_node = key_node.value
            var = self._const_str(key_node)
            if var:
                self.usage.vars.add(var)
                self.usage.calls_environ_index += 1
        self.generic_visit(node)

    # --- Call: os.getenv("VAR"), getenv("VAR"), environ.get("VAR")
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        # getenv(...) imported directly
        if isinstance(func, ast.Name) and func.id in self.getenv_aliases:
            if node.args:
                var = self._const_str(node.args[0])
                if var:
                    self.usage.vars.add(var)
                    self.usage.calls_getenv += 1
        # os.getenv(...)
        elif self._is_os_getenv_attr(func):
            if node.args:
                var = self._const_str(node.args[0])
                if var:
                    self.usage.vars.add(var)
                    self.usage.calls_getenv += 1
        # environ.get("VAR")
        elif isinstance(func, ast.Attribute) and func.attr == "get":
            value = func.value
            if self._is_environ_name(value) or self._is_os_environ_attr(value):
                if node.args:
                    var = self._const_str(node.args[0])
                    if var:
                        self.usage.vars.add(var)
                        self.usage.calls_environ_get += 1

        self.generic_visit(node)


def _scan_python_env_usage(path: Path) -> Optional[_PyEnvUsage]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    try:
        tree = ast.parse(text)
    except Exception:
        return None
    v = _PyEnvVisitor()
    v.visit(tree)
    if not v.usage.vars and v.usage.calls_getenv == 0 and v.usage.calls_environ_get == 0 and v.usage.calls_environ_index == 0:
        return None
    return v.usage


# ──────────────────────────────────────────────────────────────────────────────
# JS/TS env usage (regex)
# ──────────────────────────────────────────────────────────────────────────────

_RE_JS_PROCESS_ENV_DOT = re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)")
_RE_JS_PROCESS_ENV_BRACKET = re.compile(r"process\.env\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]")
# (Optional) Vite-style import.meta.env.VAR
_RE_JS_IMPORT_META_ENV = re.compile(r"import\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)")

_JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

def _scan_js_env_usage_text(text: str) -> Set[str]:
    out: Set[str] = set()
    for rx in (_RE_JS_PROCESS_ENV_DOT, _RE_JS_PROCESS_ENV_BRACKET, _RE_JS_IMPORT_META_ENV):
        for m in rx.finditer(text):
            out.add(m.group(1))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def scan(repo_root: Path, discovered: Iterable[RepoItem]) -> List[Dict]:
    """
    Scan the repository for environment variables in:
      - .env-like files (including templates/samples)
      - config files (YAML/JSON/TOML/INI/etc.) via placeholder patterns
      - Python source via AST (os.getenv/environ[...] etc.)
      - JS/TS via regex (process.env.* and import.meta.env.*)

    Returns a list of manifest records.
    """
    records: List[Dict] = []

    env_file_vars_all: Set[str] = set()
    cfg_vars_all: Set[str] = set()
    py_vars_all: Set[str] = set()
    js_vars_all: Set[str] = set()

    env_files_count = 0
    cfg_files_count = 0
    py_files_count = 0
    js_files_count = 0

    # Partition discovered by extension/basename
    items: List[RepoItem] = list(discovered)
    for local, rel in items:
        bn = Path(rel).name
        ext = Path(rel).suffix.lower()

        # --- .env-like
        if _is_env_like(bn):
            text = _read_text_limited(local)
            keys = _parse_env_vars(text)
            is_template = _is_template_env(bn)
            records.append({
                "kind": "env.file",
                "path": rel,
                "vars": keys,
                "count": len(keys),
                "is_template": bool(is_template),
            })
            env_files_count += 1
            env_file_vars_all.update(keys)
            continue

        # --- Config placeholders
        if ext in _CONFIG_EXTS:
            text = _read_text_limited(local)
            if ext == ".json":
                vars_ = _extract_placeholders_from_json(text)
            elif ext == ".toml":
                vars_ = _extract_placeholders_from_toml(text)
            else:
                vars_ = _extract_placeholders_from_text(text)
            if vars_:
                records.append({
                    "kind": "env.required",
                    "path": rel,
                    "vars": sorted(vars_),
                    "count": len(vars_),
                })
                cfg_files_count += 1
                cfg_vars_all.update(vars_)

        # --- Python usage
        if rel.endswith(".py"):
            usage = _scan_python_env_usage(local)
            if usage:
                vars_sorted = sorted(usage.vars)
                records.append({
                    "kind": "env.usage",
                    "language": "python",
                    "path": rel,
                    "vars": vars_sorted,
                    "count": len(vars_sorted),
                    "calls": {
                        "getenv": usage.calls_getenv,
                        "environ_index": usage.calls_environ_index,
                        "environ_get": usage.calls_environ_get,
                    },
                })
                py_files_count += 1
                py_vars_all.update(usage.vars)

        # --- JS/TS usage
        if ext in _JS_EXTS:
            text = _read_text_limited(local)
            js_vars = _scan_js_env_usage_text(text)
            if js_vars:
                records.append({
                    "kind": "env.usage",
                    "language": "javascript",
                    "path": rel,
                    "vars": sorted(js_vars),
                    "count": len(js_vars),
                    "calls": {"process_env": sum(1 for _ in _RE_JS_PROCESS_ENV_DOT.finditer(text))
                                           + sum(1 for _ in _RE_JS_PROCESS_ENV_BRACKET.finditer(text))
                                           + sum(1 for _ in _RE_JS_IMPORT_META_ENV.finditer(text))},
                })
                js_files_count += 1
                js_vars_all.update(js_vars)

    # Summary
    union_all = sorted(set().union(env_file_vars_all, cfg_vars_all, py_vars_all, js_vars_all))
    summary = {
        "kind": "env.summary",
        "files_scanned": len(items),
        "sources": {
            "env_files": env_files_count,
            "configs": cfg_files_count,
            "python": py_files_count,
            "javascript": js_files_count,
        },
        "unique_vars": {
            "env_files": sorted(env_file_vars_all),
            "configs": sorted(cfg_vars_all),
            "python": sorted(py_vars_all),
            "javascript": sorted(js_vars_all),
            "all": union_all,
        },
        "counts": {
            "env_files": len(env_file_vars_all),
            "configs": len(cfg_vars_all),
            "python": len(py_vars_all),
            "javascript": len(js_vars_all),
            "all": len(union_all),
        },
    }
    records.append(summary)
    return records


__all__ = ["scan"]
