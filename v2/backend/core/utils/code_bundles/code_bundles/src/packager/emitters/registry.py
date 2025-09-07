# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/emitters/registry.py
"""
Reducer registry for analysis families.

Responsibilities
----------------
- Provide a single place that maps canonical family names → reducer callables.
- House robust, stdlib-only reducers for key families:
    • quality       → complexity/quality rollups
    • entrypoints   → python/shell entrypoint inventory
    • env           → environment variable usage
    • deps          → dependency index summary (graceful no-data handling)
    • ast_imports   → import-edge rollup (edges emitted as edge.import)
    • io_core       → manifest header / bundle summary rollup
    • git           → repo info rollup (if present)
- Expose helpers for canonicalization and zero summaries.

Implementation principles
-------------------------
- Zero coupling to scanner internals: reducers accept *whatever* shape the manifest has.
- Be tolerant to field variations across runs (backfill-friendly).
- Never crash: return a minimal-but-useful summary when inputs are odd.
- Avoid over-fitting: keep the schema small and predictable.
- No 3rd party deps.

Public API
----------
- get_reducer(family: str) -> Callable[[list[dict]], dict]
- zero_summary_for(family: str) -> dict
- canonicalize_family(name: str) -> str
"""

from __future__ import annotations

from collections import Counter
import os
from typing import Any, Callable, Dict, List
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.core.loader import load_packager_config
from v2.backend.core.utils.code_bundles.code_bundles.execute.loader import get_repo_root

# ──────────────────────────────────────────────────────────────────────────────
# Canonicalization + Config loading
# ──────────────────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    return s.replace("-", "_").lower()


def _load_aliases_from_config() -> Dict[str, str]:
    """
    Load aliases via the central loader; merge (in order):
      1) reader.aliases        (broad normalization coming from the reader)
      2) family_aliases        (top-level anchor, if present)
      3) registry.aliases      (explicit registry overrides)
    Later entries override earlier ones. Supports dict-like or namespace-like cfg.
    """
    repo_root = get_repo_root()
    #cfg_path = (repo_root / "config" / "packager.yml").resolve()
    cfg = load_packager_config(repo_root=repo_root)

    def _as_dict(obj, key) -> Dict[str, str]:
        if obj is None:
            return {}
        if isinstance(obj, dict):
            maybe = obj.get(key)
        else:
            maybe = getattr(obj, key, None)
        return maybe if isinstance(maybe, dict) else {}

    reader_aliases = _as_dict(getattr(cfg, "reader", None), "aliases")
    fam_aliases = _as_dict(cfg, "family_aliases")
    reg_aliases = _as_dict(getattr(cfg, "registry", None), "aliases")

    merged = {}
    merged.update(reader_aliases)
    merged.update(fam_aliases)
    merged.update(reg_aliases)
    # Canonicalize values
    canon = {}
    for k, v in merged.items():
        if isinstance(k, str) and isinstance(v, str):
            canon[_norm(k)] = _norm(v)
    return canon


_FAMILY_ALIASES: Dict[str, str] = _load_aliases_from_config()


def canonicalize_family(name: str) -> str:
    """
    Canonicalize arbitrary family names:
    - lower-case
    - hyphens → underscores
    - apply alias mapping (dotted names like ast.calls → ast_calls)
    """
    n = _norm(name)
    n = n.replace(".", "_")
    return _FAMILY_ALIASES.get(n, n)


def _zero_summary_base() -> Dict[str, Any]:
    """Return a minimal zero-like summary structure."""
    return {"no_data": True, "totals": {}, "top": []}


def _top_n(counter_like: Dict[str, int], n: int = 50) -> List[Dict[str, Any]]:
    """Return top-N list of {name, count} sorted desc."""
    if not isinstance(counter_like, dict):
        return []
    items = [(k, v) for k, v in counter_like.items() if isinstance(k, str) and isinstance(v, int)]
    items.sort(key=lambda kv: (-kv[1], kv[0]))
    return [{"name": k, "count": v} for k, v in items[:n]]


# ──────────────────────────────────────────────────────────────────────────────
# Reducers (local implementations)
# ──────────────────────────────────────────────────────────────────────────────

def _reduce_generic_counter(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return _zero_summary_base()
    counts = Counter()
    for it in items:
        if isinstance(it, dict):
            for k, v in it.items():
                if isinstance(v, int):
                    counts[k] += v
    return {"no_data": False, "totals": dict(counts), "top": []}


def _reduce_quality(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return _zero_summary_base()
    totals = Counter()
    worst = Counter()
    for it in items:
        if not isinstance(it, dict):
            continue
        loc = it.get("loc") or it.get("sloc") or 0
        cyc = it.get("cyclomatic") or it.get("complexity") or 0
        if isinstance(loc, int):
            totals["loc"] += loc
        if isinstance(cyc, int):
            totals["cyclomatic"] += cyc
        name = it.get("name") or it.get("file")
        if isinstance(name, str) and isinstance(cyc, int):
            worst[name] = max(worst.get(name, 0), cyc)
    return {
        "no_data": False,
        "totals": dict(totals),
        "top": [{"name": k, "score": v} for k, v in sorted(worst.items(), key=lambda kv: (-kv[1], kv[0]))[:50]],
    }


def _reduce_quality_metric(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return _zero_summary_base()
    totals = Counter()
    for it in items:
        if not isinstance(it, dict):
            continue
        for k in ("loc", "sloc", "n_functions", "n_classes", "cyclomatic"):
            v = it.get(k)
            if isinstance(v, int):
                totals[k] += v
    return {"no_data": False, "totals": dict(totals), "top": []}


def _reduce_env(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return _zero_summary_base()
    keys = Counter()
    files = Counter()
    for it in items:
        if not isinstance(it, dict):
            continue
        k = it.get("key") or it.get("name")
        if isinstance(k, str):
            keys[k] += 1
        f = it.get("file")
        if isinstance(f, str):
            files[f] += 1
    return {
        "no_data": False,
        "totals": {"keys": sum(keys.values()), "files": len(files)},
        "top": [{"name": k, "count": v} for k, v in keys.most_common(50)],
    }


def _reduce_deps(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Graceful when no dependency files exist."""
    if not items:
        return _zero_summary_base()
    by_name = Counter()
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or it.get("package") or it.get("id")
        if isinstance(name, str):
            by_name[name] += 1
    return {"no_data": False, "totals": {"packages": len(by_name)}, "top": _top_n(dict(by_name))}


def _reduce_ast_imports(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return _zero_summary_base()
    # Count by 'module' / 'name' fallback
    modules = Counter()
    for it in items:
        if not isinstance(it, dict):
            continue
        target = it.get("module") or it.get("name") or it.get("target")
        if isinstance(target, str):
            modules[target] += 1
    return {
        "no_data": False,
        "totals": {"imports": sum(modules.values()), "unique": len(modules)},
        "top": _top_n(dict(modules)),
    }


def _reduce_ast_calls(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return _zero_summary_base()
    calls = Counter()
    for it in items:
        if not isinstance(it, dict):
            continue
        # Tolerate multiple shapes: name/call/func/callee
        name = it.get("name") or it.get("call") or it.get("func") or it.get("callee")
        if isinstance(name, str):
            calls[name] += 1
    return {
        "no_data": False,
        "calls_top": _top_n(dict(calls), n=50),
        "calls_total": sum(calls.values()),
    }


def _reduce_docs(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return _zero_summary_base()
    coverage = Counter()
    for it in items:
        if not isinstance(it, dict):
            continue
        cov = it.get("coverage") or it.get("doc_coverage")
        if isinstance(cov, int):
            coverage["coverage"] += cov
    return {"no_data": False, "metrics_sum": dict(coverage)}


def _reduce_ast_docstring(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize docstrings emitted from the manifest (backfill-friendly)."""
    if not items:
        return _zero_summary_base()
    by_owner_kind = Counter()
    owners = Counter()
    files = Counter()
    for it in items:
        if not isinstance(it, dict):
            continue
        ok = _norm(it.get("owner_kind") or it.get("kind") or "")
        if ok:
            by_owner_kind[ok] += 1
        owner = it.get("owner") or it.get("symbol") or it.get("name")
        if isinstance(owner, str):
            owners[owner] += 1
        f = it.get("file")
        if isinstance(f, str):
            files[f] += 1
    return {
        "no_data": False,
        "totals": {"docstrings": len(items)},
        "by_owner_kind": _top_n(dict(by_owner_kind)),
        "top_owners": _top_n(dict(owners)),
        "files_top": _top_n(dict(files)),
    }


def _reduce_static(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return _zero_summary_base()
    # Keep schema consistent with other summaries by nesting under totals.
    return {"no_data": False, "totals": {"count": len(items)}}


def _reduce_io_core(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return _zero_summary_base()
    headers = []
    bundles = []
    for it in items:
        if not isinstance(it, dict):
            continue
        h = it.get("manifest_header")
        b = it.get("bundle_summary")
        if isinstance(h, dict):
            headers.append(h)
        if isinstance(b, dict):
            bundles.append(b)
    return {
        "no_data": False,
        "header": headers[0] if headers else {},
        "bundle_summaries": bundles[:5],
    }


def _reduce_git(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return _zero_summary_base()
    info = {}
    for it in items:
        if isinstance(it, dict):
            info.update(it)
    return {"no_data": False, "info": info}


def _reduce_entrypoints(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return _zero_summary_base()
    py = []
    sh = []
    for it in items:
        if not isinstance(it, dict):
            continue
        k = _norm(it.get("kind") or it.get("type") or "")
        # Derive sensible defaults when fields are missing.
        f = it.get("file") if isinstance(it.get("file"), str) else None
        derived_name = os.path.basename(f) if f else None
        name = it.get("name") or derived_name
        target = it.get("target") or f
        # If kind is missing but interpreter is present, assume shell.
        if not k and isinstance(it.get("interpreter"), str):
            k = "shell"
        if k in {"python", "py"}:
            py.append({"name": name, "target": target})
        elif k in {"shell", "sh", "bash"}:
            sh.append({"name": name, "target": target})
    return {"no_data": False, "python": py, "shell": sh, "total": len(py) + len(sh)}


def _reduce_license(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """License header rollup tolerant to minimal fields."""
    if not items:
        return _zero_summary_base()
    spdx = Counter()
    with_header = 0
    without_header = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = it.get("spdx_id") or it.get("spdx") or it.get("id")
        if isinstance(sid, str):
            spdx[sid] += 1
        hh = it.get("has_header")
        if hh is True:
            with_header += 1
        elif hh is False:
            without_header += 1
    return {
        "no_data": False,
        "totals": {
            "files": len(items),
            "with_header": with_header,
            "without_header": without_header,
        },
        "spdx_top": _top_n(dict(spdx)),
    }

def _reduce_license_header(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Alias reducer for license_header token."""
    return _reduce_license(items)

# ──────────────────────────────────────────────────────────────────────────────
# Registry (bindings imported entirely from packager.yml; no hard-coded list)
# ──────────────────────────────────────────────────────────────────────────────

def _load_reducers_from_config() -> Dict[str, Callable[[List[Dict[str, Any]]], Dict[str, Any]]]:
    """
    Load reducer bindings from config:
      packager.yml → registry.reducers: { <family>: <token> }

    Binding rules:
      - Family keys are canonicalized with canonicalize_family (dots→underscores, aliases applied).
      - Token 'X' resolves to a local callable named _reduce_<X> (after lower/underscore normalization).
      - Unknown tokens fall back to _reduce_generic_counter.
      - For AST families only, also bind common aliases: singular/plural and dotted form.
    """
    repo_root = get_repo_root()
    cfg = load_packager_config(repo_root=repo_root)

    # Extract the reducers mapping regardless of shape
    reg = getattr(cfg, "registry", None)
    if isinstance(reg, dict):
        mapping = reg.get("reducers") or {}
    else:
        mapping = getattr(reg, "reducers", {}) or {}
    if not isinstance(mapping, dict):
        mapping = {}

    bound: Dict[str, Callable[[List[Dict[str, Any]]], Dict[str, Any]]] = {}

    def _bind(family_raw: str, token_raw: str) -> None:
        # Canonicalize family so lookups hit the same key space.
        fam_c = canonicalize_family(str(family_raw))            # e.g. "ast.calls" → "ast_calls"
        token_c = _norm(str(token_raw)).replace(".", "_")       # e.g. "ast.calls" → "ast_calls"
        func = globals().get(f"_reduce_{token_c}")
        reducer = func if callable(func) else _reduce_generic_counter

        # Primary binding
        bound[fam_c] = reducer

        # AST-only aliases: singular/plural + dotted, to catch emitter/file naming variants.
        if fam_c.startswith("ast_"):
            # singular/plural alias
            if fam_c.endswith("s"):
                fam_sing = fam_c[:-1]
                bound.setdefault(fam_sing, reducer)
            else:
                bound.setdefault(f"{fam_c}s", reducer)
            # dotted alias (ast_calls ↔ ast.calls)
            dotted = fam_c.replace("_", ".")
            bound.setdefault(dotted, reducer)

    for fam, token in mapping.items():
        _bind(fam, token)

    return bound

# Single source of truth for reducer bindings (from YAML)
_REDUCERS: Dict[str, Callable[[List[Dict[str, Any]]], Dict[str, Any]]] = _load_reducers_from_config()


def get_reducer(family: str) -> Callable[[List[Dict[str, Any]]], Dict[str, Any]]:
    """Return the reducer for a canonical family; generic counter if unknown."""
    fam = canonicalize_family(family)
    return _REDUCERS.get(fam, _reduce_generic_counter)


def zero_summary_for(family: str) -> dict:
    """Schema-agnostic zero summary (no hard-coded per-family shapes)."""
    fam = canonicalize_family(family)
    return {"family": fam, "stats": {"count": 0}, "items": []}

