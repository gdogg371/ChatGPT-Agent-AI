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
    "asset.file": "asset",
    "asset.summary": "asset",
    "git_info": "git",
    "license_scan": "license",
    "secrets_scan": "secrets",
    "env_index": "env",
    "deps_index": "deps",
    "deps.index": "deps",
    "deps.index.summary": "deps",   # summary rows → deps
    "deps_index_summary": "deps",   # underscored variant
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
    kinds = Counter(
        _str_or_unknown(_vget(it, ("payload", "kind"), ("kind",), ("type",)))
        for it in items
    )
    return {"family": "ast_symbols", "stats": {"count": len(items), "kinds": dict(kinds)}}


def _ast_imports_reducer(items: List[dict]) -> dict:
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
    has_summary = any(
        _vget(it, ("packages_unique",), ("payload","packages_unique")) is not None
        or _vget(it, ("top_packages",), ("payload","top_packages")) is not None
        for it in items
    )

    if has_summary:
        pkgs_unique_vals: List[int] = []
        top_pairs: Counter = Counter()
        ecosystems: Counter = Counter()
        manifests: Counter = Counter()

        files_total = 0
        lockfiles_total = 0
        lockfiles_by_kind: Counter = Counter()

        def _as_pairs(v) -> List[Tuple[str,int]]:
            out = []
            if isinstance(v, list):
                for el in v:
                    if isinstance(el, (list, tuple)) and len(el) == 2:
                        name, cnt = el
                    elif isinstance(el, dict):
                        name = el.get("name") or el.get("pkg") or el.get("package") or el.get("id")
                        cnt = el.get("count") or el.get("n") or el.get("num") or 1
                    else:
                        continue
                    try: cnt = int(cnt)
                    except Exception: cnt = 1
                    name = _str_or_unknown(name)
                    if name and name != "unknown":
                        out.append((name, cnt))
            elif isinstance(v, dict):
                for k, cnt in v.items():
                    try: top = int(cnt)
                    except Exception: top = 1
                    out.append((_str_or_unknown(k), top))
            return out

        for it in items:
            # packages_unique/top_packages
            pu = _vget(it, ("packages_unique",), ("payload","packages_unique"))
            if isinstance(pu, (int, float)): pkgs_unique_vals.append(int(pu))
            tp = _vget(it, ("top_packages",), ("payload","top_packages"))
            for name, cnt in _as_pairs(tp): top_pairs[name] += int(cnt)

            # ecosystems/manifests (dicts)
            ecs = _vget(it, ("ecosystems",), ("payload","ecosystems"))
            if isinstance(ecs, dict):
                for k,v in ecs.items():
                    try: ecosystems[_str_or_unknown(k)] += int(v)
                    except Exception: ecosystems[_str_or_unknown(k)] += 1

            man = _vget(it, ("manifests",), ("payload","manifests"))
            if isinstance(man, dict):
                for k,v in man.items():
                    try: manifests[_str_or_unknown(k)] += int(v)
                    except Exception: manifests[_str_or_unknown(k)] += 1

            # NEW: files / lockfiles
            f = _vget(it, ("files",), ("payload","files"))
            if isinstance(f, (int, float)): files_total += int(f)
            lf = _vget(it, ("lockfiles",), ("payload","lockfiles"))
            if isinstance(lf, dict):
                c = lf.get("count")
                if isinstance(c, (int, float)): lockfiles_total += int(c)
                bk = lf.get("by_kind")
                if isinstance(bk, dict):
                    for k,v in bk.items():
                        try: lockfiles_by_kind[_str_or_unknown(k)] += int(v)
                        except Exception: lockfiles_by_kind[_str_or_unknown(k)] += 1

        return {
            "family": "deps",
            "stats": {
                "rows": len(items),
                "packages_unique": max(pkgs_unique_vals) if pkgs_unique_vals else 0,
                "top_packages": top_pairs.most_common(50),
                # Keep dict shapes for fidelity (you can switch to list-of-pairs if preferred)
                "ecosystems": dict(ecosystems),
                "manifests": dict(manifests),
                # NEW
                "files": files_total,
                "lockfiles": {
                    "count": lockfiles_total,
                    "by_kind": dict(lockfiles_by_kind),
                },
            },
        }



def _entrypoints_reducer(items: List[dict]) -> dict:
    kinds = Counter(
        _str_or_unknown(_vget(it, ("payload", "kind"), ("kind",), ("type",)))
        for it in items
    )
    return {"family": "entrypoints", "stats": {"count": len(items), "kinds": dict(kinds)}}


def _docs_reducer(items: List[dict]) -> dict:
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
    vals: List[float] = []
    for it in items:
        v = _vget(it, ("payload", "complexity"), ("complexity",), ("radon_cc",))
        try:
            if v is not None:
                vals.append(float(v))
        except Exception:
            pass
    avg = sum(vals) / max(1, len(vals)) if vals else 0.0
    return {"family": "quality", "stats": {"count": len(vals), "avg_complexity": round(avg, 3)}}


def _sql_reducer(items: List[dict]) -> dict:
    kinds = Counter(
        _str_or_unknown(_vget(it, ("payload", "kind"), ("kind",), ("type",)))
        for it in items
    )
    return {"family": "sql", "stats": {"count": len(items), "kinds": dict(kinds)}}


def _asset_reducer(items: List[dict]) -> dict:
    # Show top file extensions, MIME types and categories if present
    exts = Counter(_str_or_unknown(_vget(it, ("payload", "ext"), ("ext",))) for it in items)
    mimes = Counter(_str_or_unknown(_vget(it, ("payload", "mime"), ("mime",))) for it in items)
    cats = Counter(_str_or_unknown(_vget(it, ("payload", "category"), ("category",))) for it in items)
    return {
        "family": "asset",
        "stats": {
            "count": len(items),
            "top_ext": exts.most_common(15),
            "top_mime": mimes.most_common(15),
            "top_category": cats.most_common(15),
        },
    }


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
    "asset": _asset_reducer,
    # Fallbacks (generic summaries)
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





