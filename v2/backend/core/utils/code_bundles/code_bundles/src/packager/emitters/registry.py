from __future__ import annotations

from collections import Counter
from typing import Callable, Dict, List, Any, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# Aliases (scanner → canonical family)
# ──────────────────────────────────────────────────────────────────────────────

_ALIASES: Dict[str, str] = {
    # AST dotted/variants
    "ast.call": "ast_calls",
    "ast.calls": "ast_calls",
    "call": "ast_calls",
    "ast.symbol": "ast_symbols",
    "ast.symbols": "ast_symbols",
    "file": "ast_symbols",
    "class": "ast_symbols",
    "function": "ast_symbols",
    "ast.import": "ast_imports",
    "ast.imports": "ast_imports",
    "ast.xref": "ast_imports",
    "import": "ast_imports",
    "import_from": "ast_imports",
    "ast.import_from": "ast_imports",
    "from": "ast_imports",
    "edge.import": "ast_imports",
    "ast.docstring": "docs",

    # Scanner → canonical
    "js_ts": "js",
    "owners_index": "codeowners",
    "assets": "asset",
    "asset.index": "asset",
    "git_info": "git",
    "license_scan": "license",
    "secrets_scan": "secrets",
    "env_index": "env",
    "deps_index": "deps",
    "html_index": "html",
    "sql.index": "sql",
    "sql_index": "sql",

    # Docs/quality variants
    "docs.coverage": "docs",
    "doc_coverage": "docs",
    "quality.complexity": "quality",

    # IO/core
    "artifact": "io_core",
    "manifest": "io_core",
    "manifest.header": "io_core",
    "manifest.summary": "io_core",
    "module_index": "io_core",
}


def canonicalize_family(name: str) -> str:
    if name in _ALIASES:
        return _ALIASES[name]
    dotted = name.replace(".", "_")
    if dotted in _ALIASES:
        return _ALIASES[dotted]
    return dotted


# ──────────────────────────────────────────────────────────────────────────────
# Helpers to read both top-level and payload.* shapes
# ──────────────────────────────────────────────────────────────────────────────

def _get_in(obj: Any, path: Tuple[str, ...]) -> Any:
    cur = obj
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def _vget(rec: Dict[str, Any], *candidates: Tuple[str, ...]) -> Any:
    """
    Return the first non-None value for any of the candidate key-paths.
    Each candidate is a tuple like ('payload','name') or ('callee',).
    """
    for path in candidates:
        val = _get_in(rec, path)
        if val is not None:
            return val
    return None


def _str_or_unknown(x: Any) -> str:
    s = x if isinstance(x, str) else (str(x) if x is not None else "")
    s = s.strip()
    return s if s else "unknown"


# ──────────────────────────────────────────────────────────────────────────────
# Reducers (robust to top-level vs payload.* fields)
# ──────────────────────────────────────────────────────────────────────────────

def _ast_symbols_reducer(items: List[dict]) -> dict:
    # Prefer payload.kind, then top-level kind/type
    kinds = Counter(
        _str_or_unknown(
            _vget(it, ("payload", "kind"), ("kind",), ("type",))
        )
        for it in items
    )
    return {"family": "ast_symbols", "stats": {"count": len(items), "kinds": dict(kinds)}}


def _ast_imports_reducer(items: List[dict]) -> dict:
    # Try module/target/import/from_module across payload and top-level
    def _mod(it: dict) -> str:
        return _str_or_unknown(
            _vget(
                it,
                ("payload", "module"),
                ("module",),
                ("payload", "target"),
                ("target",),
                ("payload", "import"),
                ("import",),
                ("payload", "from_module"),
                ("from_module",),
            )
        )
    modules = Counter(_mod(it) for it in items)
    return {"family": "ast_imports", "stats": {"count": len(items), "top_modules": modules.most_common(15)}}


def _ast_calls_reducer(items: List[dict]) -> dict:
    # Prefer explicit callee/name/attr across payload and top-level
    def _call_name(it: dict) -> str:
        return _str_or_unknown(
            _vget(
                it,
                ("payload", "name"),
                ("payload", "callee"),
                ("callee",),
                ("name",),
                ("attr",),
                ("func",),
            )
        )
    names = Counter(_call_name(it) for it in items)
    return {"family": "ast_calls", "stats": {"count": len(items), "top_calls": names.most_common(25)}}


def _deps_reducer(items: List[dict]) -> dict:
    pkgs = Counter(
        _str_or_unknown(
            _vget(it, ("payload", "name"), ("name",), ("package",))
        )
        for it in items
    )
    return {"family": "deps", "stats": {"count": len(items), "packages": pkgs.most_common(50)}}


def _entrypoints_reducer(items: List[dict]) -> dict:
    kinds = Counter(
        _str_or_unknown(
            _vget(it, ("payload", "kind"), ("kind",), ("type",))
        )
        for it in items
    )
    return {"family": "entrypoints", "stats": {"count": len(items), "kinds": dict(kinds)}}


def _docs_reducer(items: List[dict]) -> dict:
    # Accept coverage at payload.coverage, coverage, or docstring_coverage
    vals: List[float] = []
    for it in items:
        v = _vget(it, ("payload", "coverage"), ("coverage",), ("docstring_coverage",))
        try:
            if v is not None:
                vals.append(float(v))
        except Exception:
            pass
    avg = sum(vals) / max(1, len(vals)) if vals else 0.0
    return {"family": "docs", "stats": {"count": len(items), "avg_coverage": round(avg, 3)}}


def _quality_reducer(items: List[dict]) -> dict:
    # Accept complexity at payload.complexity, complexity, or radon_cc
    vals: List[float] = []
    for it in items:
        v = _vget(it, ("payload", "complexity"), ("complexity",), ("radon_cc",))
        try:
            if v is not None:
                vals.append(float(v))
        except Exception:
            pass
    avg = sum(vals) / max(1, len(vals)) if vals else 0.0
    return {"family": "quality", "stats": {"count": len(items), "avg_complexity": round(avg, 3)}}


def _sql_reducer(items: List[dict]) -> dict:
    kinds = Counter(
        _str_or_unknown(
            _vget(it, ("payload", "kind"), ("kind",), ("type",))
        )
        for it in items
    )
    return {"family": "sql", "stats": {"count": len(items), "kinds": dict(kinds)}}


def _generic_counter(items: List[dict], family: str) -> dict:
    # Keep this minimal; no payload echo to avoid large sidecars
    return {"family": family, "stats": {"count": len(items)}}


# Map family → reducer
_REDUCERS: Dict[str, Callable[[List[dict]], dict]] = {
    "ast_symbols": _ast_symbols_reducer,
    "ast_imports": _ast_imports_reducer,
    "ast_calls": _ast_calls_reducer,
    "deps": _deps_reducer,
    "entrypoints": _entrypoints_reducer,
    "docs": _docs_reducer,
    "quality": _quality_reducer,
    "sql": _sql_reducer,
    # Fallbacks (generic summaries)
    "asset": lambda x: _generic_counter(x, "asset"),
    "env": lambda x: _generic_counter(x, "env"),
    "git": lambda x: _generic_counter(x, "git"),
    "license": lambda x: _generic_counter(x, "license"),
    "secrets": lambda x: _generic_counter(x, "secrets"),
    "html": lambda x: _generic_counter(x, "html"),
    "js": lambda x: _generic_counter(x, "js"),
    "cs": lambda x: _generic_counter(x, "cs"),
    "sbom": lambda x: _generic_counter(x, "sbom"),
    "io_core": lambda x: _generic_counter(x, "io_core"),
    "codeowners": lambda x: _generic_counter(x, "codeowners"),
}


def get_reducer(family: str):
    return _REDUCERS.get(family, lambda x: _generic_counter(x, family))


def zero_summary_for(family: str) -> dict:
    return {"family": family, "stats": {"count": 0}, "items": []}


