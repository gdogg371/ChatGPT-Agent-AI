from __future__ import annotations

from collections import Counter
from typing import Callable, Dict, List

# Canonical family names we expect to write sidecars for
_CANON_FAMILIES = {
    # Priority
    "asset", "deps", "entrypoints", "env", "git", "license", "secrets", "sql",
    # AST
    "ast_symbols", "ast_imports", "ast_calls",
    # Docs / Quality
    "docs", "quality",
    # Polyglot
    "html", "js", "cs",
    # Supply chain
    "sbom",
    # IO/Core synth (manifest-only by default)
    "io_core",
    # Additional family
    "codeowners",
}

# Expanded aliasing to absorb plugin scanner families into canonical keys
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
    "import": "ast_imports",
    "import_from": "ast_imports",
    "ast.import_from": "ast_imports",
    "from": "ast_imports",
    "edge.import": "ast_imports",

    # Scanner → canonical
    "js_ts": "js",
    "owners_index": "codeowners",
    "assets": "asset",
    "git_info": "git",
    "license_scan": "license",
    "secrets_scan": "secrets",
    "env_index": "env",
    "deps_index": "deps",
    "html_index": "html",
    "sql_index": "sql",

    # Docs/quality variants
    "docs.coverage": "docs",
    "doc_coverage": "docs",
    "quality.complexity": "quality",

    # IO/core
    "artifact": "io_core",
    "manifest": "io_core",
}


def canonicalize_family(name: str) -> str:
    if name in _ALIASES:
        return _ALIASES[name]
    dotted = name.replace(".", "_")
    if dotted in _ALIASES:
        return _ALIASES[dotted]
    return dotted


def _generic_counter(items: List[dict], family: str) -> dict:
    return {
        "family": family,
        "stats": {"count": len(items)},
        "sample": items[:5],  # tiny peek only for debug
    }


def _ast_symbols_reducer(items: List[dict]) -> dict:
    kinds = Counter([x.get("payload", {}).get("kind", "unknown") for x in items])
    return {"family": "ast_symbols", "stats": {"count": len(items), "kinds": dict(kinds)}}


def _ast_imports_reducer(items: List[dict]) -> dict:
    modules = Counter([x.get("payload", {}).get("module", "unknown") for x in items])
    return {"family": "ast_imports", "stats": {"count": len(items), "top_modules": modules.most_common(10)}}


def _ast_calls_reducer(items: List[dict]) -> dict:
    names = Counter([x.get("payload", {}).get("name", "unknown") for x in items])
    return {"family": "ast_calls", "stats": {"count": len(items), "top_calls": names.most_common(15)}}


def _deps_reducer(items: List[dict]) -> dict:
    pkgs = Counter([x.get("payload", {}).get("name", "unknown") for x in items])
    return {"family": "deps", "stats": {"count": len(items), "packages": pkgs.most_common(50)}}


def _entrypoints_reducer(items: List[dict]) -> dict:
    kinds = Counter([x.get("payload", {}).get("kind", "unknown") for x in items])
    return {"family": "entrypoints", "stats": {"count": len(items), "kinds": dict(kinds)}}


def _docs_reducer(items: List[dict]) -> dict:
    vals = [
        x.get("payload", {}).get("coverage", 0.0)
        for x in items
        if isinstance(x.get("payload", {}).get("coverage", None), (int, float))
    ]
    avg = sum(vals) / max(1, len(vals)) if vals else 0.0
    return {"family": "docs", "stats": {"count": len(items), "avg_coverage": round(avg, 3)}}


def _quality_reducer(items: List[dict]) -> dict:
    vals = [
        x.get("payload", {}).get("complexity", 0)
        for x in items
        if isinstance(x.get("payload", {}).get("complexity", None), (int, float))
    ]
    avg = sum(vals) / max(1, len(vals)) if vals else 0.0
    return {"family": "quality", "stats": {"count": len(items), "avg_complexity": round(avg, 3)}}


def _sql_reducer(items: List[dict]) -> dict:
    kinds = Counter([x.get("payload", {}).get("kind", "unknown") for x in items])
    return {"family": "sql", "stats": {"count": len(items), "kinds": dict(kinds)}}


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

