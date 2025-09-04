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

Design notes
------------
- Deterministic output: stable sorts, explicit rounding.
- Accept heterogeneous item shapes seen in manifests.
- Avoid emitting misleading empty collections: mark `no_data: true` where appropriate.

Public API
----------
- get_reducer(family: str) -> Callable[[list[dict]], dict]
- zero_summary_for(family: str) -> dict
- canonicalize_family(name: str) -> str
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Callable, Dict, List, Optional


# ──────────────────────────────────────────────────────────────────────────────
# Canonicalization
# ──────────────────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    return s.replace("-", "_").lower()


# Keep this aligned with ManifestReader’s aliases; harmless to be a superset.
_ALIASES: Dict[str, str] = {
    # AST variants
    "ast.call": "ast_calls",
    "ast.calls": "ast_calls",
    "call": "ast_calls",
    "ast.symbol": "ast_symbols",
    "ast.symbols": "ast_symbols",
    "file": "ast_symbols",
    "class": "ast_symbols",
    "function": "ast_symbols",
    "method": "ast_symbols",

    # Imports
    "import": "ast_imports",
    "ast.import": "ast_imports",
    "ast.imports": "ast_imports",
    "edge.import": "ast_imports",

    # Entrypoints
    "entrypoint": "entrypoints",
    "entrypoints": "entrypoints",
    "entrypoint.python": "entrypoints",
    "entrypoint.shell": "entrypoints",

    # JS
    "js": "js",
    "js.index": "js",

    # IO / manifest
    "io": "io_core",
    "manifest": "io_core",
    "manifest_header": "io_core",
    "bundle_summary": "io_core",

    # SBOM / deps
    "deps": "deps",
    "dep": "deps",
    "deps.index": "deps",
    "deps.index.summary": "deps",
    "sbom": "sbom",

    # Secrets
    "secret": "secrets",

    # Env
    "env": "env",
    "env.vars": "env",
    "env.usage": "env",

    # Quality
    "quality": "quality",
    "quality.metric": "quality",
    "quality_metrics": "quality",
    "quality.complexity": "quality",
    "quality_complexity": "quality",

    # SQL
    "sql": "sql",
    "sql.index": "sql",
    "sqlindex": "sql",

    # Ownership / licensing / html / git
    "codeowners": "codeowners",
    "license": "license",
    "html": "html",
    "git": "git",
    "git.info": "git",

    # Assets / misc
    "asset": "asset",
    "asset.file": "asset",
    "cs": "cs",
    "docs.coverage": "docs.coverage",
    "docs.coverage.summary": "docs.coverage",
    "ast.xref": "ast.xref",
    "module_index": "module_index",
}


def canonicalize_family(name: str) -> str:
    """Map dotted/variant names to canonical family names."""
    k = _norm(name)
    return _ALIASES.get(k, k)


# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────

def _round3(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 3)


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _str(v: Any, default: str = "unknown") -> str:
    if v is None:
        return default
    s = str(v).strip()
    return s if s else default


def _stable_top(items: List[dict], key_fn, limit: int) -> List[dict]:
    # Stable by original order, then sort by key descending
    with_index = [(i, it) for i, it in enumerate(items)]
    with_index.sort(key=lambda t: (key_fn(t[1]), -t[0]), reverse=True)
    return [it for _, it in with_index[:limit]]


# ──────────────────────────────────────────────────────────────────────────────
# Reducers
# ──────────────────────────────────────────────────────────────────────────────

def _generic_counter(items: List[dict], family: str) -> dict:
    """Fallback reducer: count items; include a tiny sample of paths if present."""
    paths = []
    for it in items:
        p = it.get("path") or it.get("file") or it.get("module")
        if p:
            paths.append(str(p))
    sample = paths[:50]
    return {
        "family": family,
        "stats": {"count": len(items), "files_with_items": len(set(paths))},
        "items": [{"path": p} for p in sample],
    }


def _reduce_quality(items: List[dict]) -> dict:
    """
    Aggregate quality/complexity:
    - Accept either per-function (by_function) or per-file (quality.metric with cyclomatic/n_functions).
    - Compute files, functions_measured, total_complexity, avg_complexity (per function),
      p95/max (when function-level data available), and heavy_files_top.
    """
    func_scores: List[int] = []
    file_agg: Dict[str, Dict[str, int]] = defaultdict(lambda: {"functions": 0, "total": 0, "max": 0})

    # Gather from heterogeneous shapes
    for it in items:
        path = _str(it.get("path"))
        by_fn = it.get("by_function")
        if isinstance(by_fn, list) and by_fn:
            for f in by_fn:
                c = _safe_int(f.get("complexity"), 1)
                func_scores.append(c)
                file_agg[path]["functions"] += 1
                file_agg[path]["total"] += c
                if c > file_agg[path]["max"]:
                    file_agg[path]["max"] = c
            continue

        # quality.metric per-file shape
        if "cyclomatic" in it or "n_functions" in it:
            total_cyc = _safe_int(it.get("cyclomatic"), 0)
            n_funcs = _safe_int(it.get("n_functions"), 0)
            if n_funcs > 0:
                # If only per-file available, approximate by distributing total equally
                avg_c = max(1, round(total_cyc / n_funcs)) if total_cyc > 0 else 1
                for _ in range(n_funcs):
                    func_scores.append(avg_c)
            file_agg[path]["functions"] += n_funcs
            file_agg[path]["total"] += total_cyc
            if n_funcs > 0:
                file_agg[path]["max"] = max(file_agg[path]["max"], avg_c)

    files = len(file_agg)
    functions_measured = sum(v["functions"] for v in file_agg.values())
    total_complexity = sum(v["total"] for v in file_agg.values())

    # Stats
    avg_complexity = None
    p95 = None
    maxc = None
    if functions_measured > 0:
        avg_complexity = total_complexity / functions_measured
    if func_scores:
        func_scores.sort()
        n = len(func_scores)
        p95 = float(func_scores[int(max(0, min(n - 1, round(0.95 * n) - 1)))])
        maxc = func_scores[-1]

    # Heavy files list
    heavy = []
    for p, v in file_agg.items():
        f = v["functions"] or 1
        heavy.append({
            "path": p,
            "functions": v["functions"],
            "total_complexity": v["total"],
            "avg_complexity": _round3(v["total"] / f),
            "max_function_complexity": v["max"] or None
        })
    heavy = _stable_top(
        heavy,
        key_fn=lambda d: (d["total_complexity"], d["max_function_complexity"] or 0),
        limit=50
    )

    return {
        "family": "quality",
        "metric": "cyclomatic_complexity_approx",
        "stats": {
            "files": files,
            "functions_measured": functions_measured,
            "total_complexity": total_complexity,
            "avg_complexity": _round3(avg_complexity),
            "p95_function_complexity": _round3(p95),
            "max_function_complexity": maxc,
        },
        "heavy_files_top": heavy,
    }


def _reduce_entrypoints(items: List[dict]) -> dict:
    """
    Build an entrypoints summary from entrypoint.python / entrypoint.shell items.
    """
    kind_counter = Counter()
    files = set()
    out_items: List[dict] = []

    for it in items:
        kind = _str(it.get("kind"), "unknown")
        path = _str(it.get("path"))
        files.add(path)
        kind_counter[kind] += 1

        rec = {"path": path, "kind": kind}
        if "module" in it:
            rec["module"] = it.get("module")
        if "has_main_fn" in it:
            rec["has_main_fn"] = it.get("has_main_fn")
        if "interpreter" in it:
            rec["interpreter"] = it.get("interpreter")
        out_items.append(rec)

    # Deterministic ordering
    out_items.sort(key=lambda d: (d["kind"], d["path"], d.get("module") or ""))

    return {
        "family": "entrypoints",
        "stats": {
            "count": len(items),
            "files": len(files),
            "by_kind": dict(kind_counter),
            "python": sum(c for k, c in kind_counter.items() if "python" in k),
            "shell": sum(c for k, c in kind_counter.items() if "shell" in k),
        },
        "items": out_items[:500],
    }


def _reduce_env(items: List[dict]) -> dict:
    """
    Summarize environment variable usage from env.usage records.
    """
    var_files = Counter()      # var -> number of files referencing it
    file_refs = Counter()      # file -> number of env refs (items)
    total_refs = 0
    out_items: List[dict] = []

    # Track which vars we've counted per file for var_files
    seen_per_file: Dict[str, set] = defaultdict(set)

    for it in items:
        path = _str(it.get("path"))
        vars_ = it.get("vars") or []
        calls = it.get("calls") or {}
        count = _safe_int(it.get("count"), 0)
        lang = it.get("language")

        total_refs += count
        file_refs[path] += 1

        # Unique vars per file
        for v in vars_:
            if v not in seen_per_file[path]:
                var_files[v] += 1
                seen_per_file[path].add(v)

        out_items.append({
            "path": path,
            "vars": list(vars_),
            "calls": dict(calls),
            "count": count,
            "language": lang,
        })

    # Deterministic ordering
    out_items.sort(key=lambda d: (d["path"], tuple(d.get("vars") or ())))

    top_vars = [{"name": name, "files": cnt} for name, cnt in var_files.most_common(100)]

    return {
        "family": "env",
        "stats": {
            "items": len(items),
            "files": len(file_refs),
            "vars_unique": len(var_files),
            "total_refs": total_refs,
        },
        "top_vars": top_vars,
        "items": out_items[:500],
    }


def _reduce_deps(items: List[dict]) -> dict:
    """
    Summarize dependencies when per-dep rows are present.
    If only placeholder/summary rows exist (e.g., deps.index.summary with empty maps),
    emit a minimal, non-misleading summary with `no_data: true`.
    """
    placeholder_only = True
    ecosystems: Counter[str] = Counter()
    manifests: Counter[str] = Counter()
    lockfiles_by_kind: Counter[str] = Counter()
    packages = Counter()
    files = set()

    for it in items:
        # Heuristic: if any substantive dep fields are present, it's not a placeholder
        if any(k in it for k in ("ecosystem", "purl_type", "manifest", "manifest_path", "lockfile", "package", "name", "version")):
            placeholder_only = False

        p = it.get("path")
        if p:
            files.add(str(p))

        eco = it.get("ecosystem") or it.get("purl_type") or it.get("language")
        if eco:
            ecosystems[str(eco)] += 1

        man = it.get("manifest") or it.get("manifest_path")
        if man:
            manifests[str(man)] += 1

        lf = it.get("lockfile") or it.get("lockfile_kind")
        if lf:
            lockfiles_by_kind[str(lf)] += 1

        name = it.get("package") or it.get("name")
        if name:
            packages[str(name)] += 1

    if placeholder_only and not ecosystems and not manifests and not packages:
        return {
            "family": "deps",
            "no_data": True,
            "stats": {
                "files": 0,
                "packages_unique": 0,
                "ecosystems": {},
                "lockfiles": {"by_kind": {}, "count": 0},
                "manifests": {},
                "top_packages": [],
            },
        }

    top_packages = [{"name": n, "count": c} for n, c in packages.most_common(50)]
    return {
        "family": "deps",
        "stats": {
            "files": len(files),
            "packages_unique": len(packages),
            "ecosystems": dict(ecosystems),
            "lockfiles": {"by_kind": dict(lockfiles_by_kind), "count": sum(lockfiles_by_kind.values())},
            "manifests": dict(manifests),
            "top_packages": top_packages,
        },
    }


def _reduce_ast_imports(items: List[dict]) -> dict:
    """
    Summarize import edges.

    Accepts multiple item shapes. In your pipeline, import edges are typically:
      {"record_type":"edge.import", "src_path":"<file>", "dst_module":"<module>", ...}

    We also tolerate alternative keys seen in other runs:
      - file path:  path | file | src_file | source | src_path
      - module:     dst_module | import_module | to | imported | target | module | name
    """
    from collections import Counter

    count = 0
    top_modules = Counter()
    top_files = Counter()

    for it in items:
        count += 1

        # File/source path
        path = (
            it.get("src_path")
            or it.get("path")
            or it.get("file")
            or it.get("src_file")
            or it.get("source")
        )
        if path:
            top_files[str(path)] += 1

        # Module/import target
        mod = (
            it.get("dst_module")
            or it.get("import_module")
            or it.get("to")
            or it.get("imported")
            or it.get("target")
            or it.get("module")
            or it.get("name")
        )
        if mod:
            top_modules[str(mod)] += 1

    return {
        "family": "ast_imports",
        "stats": {
            "count": count,
            "files": len(top_files),
            "modules": len(top_modules),
        },
        "top_modules": [{"name": n, "edges": c} for n, c in top_modules.most_common(100)],
        "top_files": [{"path": n, "edges": c} for n, c in top_files.most_common(100)],
    }


def _reduce_io_core(items: List[dict]) -> dict:
    """
    Roll up manifest header / bundle summary information (aliased to io_core).
    We try to surface stable totals: modules, metrics, edges, artifacts, and basic mode metadata.
    """
    totals = {"modules": 0, "metrics": 0, "edges": 0, "artifacts": 0}
    modes = Counter()
    parts = 0

    for it in items:
        # Some records carry aggregates
        for k in ("modules", "metrics", "edges", "artifacts"):
            if k in it:
                try:
                    totals[k] = max(totals[k], int(it.get(k) or 0))
                except Exception:
                    pass
        # Path mode / run mode
        pm = it.get("path_mode") or it.get("mode") or it.get("run_mode")
        if pm:
            modes[str(pm)] += 1
        # Chunking info
        if "parts" in it:
            try:
                parts = max(parts, int(it.get("parts") or 0))
            except Exception:
                pass

    return {
        "family": "io_core",
        "stats": {
            "modules": totals["modules"],
            "metrics": totals["metrics"],
            "edges": totals["edges"],
            "artifacts": totals["artifacts"],
            "parts": parts,
            "path_modes": dict(modes),
        },
    }


def _reduce_git(items: List[dict]) -> dict:
    """
    Summarize git info items, if present. If no items exist (e.g., snapshot build),
    the backfill will write a zero summary.
    """
    commits = []
    branches = Counter()
    remotes = Counter()
    tags = Counter()
    dirty_any = False

    for it in items:
        c = it.get("commit") or it.get("sha") or it.get("hash")
        if c:
            commits.append(str(c))
        b = it.get("branch") or it.get("ref")
        if b:
            branches[str(b)] += 1
        r = it.get("remote") or it.get("origin")
        if r:
            remotes[str(r)] += 1
        t = it.get("tag") or it.get("describe")
        if t:
            tags[str(t)] += 1
        if bool(it.get("dirty")):
            dirty_any = True

    latest = commits[0] if commits else None
    return {
        "family": "git",
        "stats": {
            "items": len(items),
            "branches": len(branches),
            "remotes": len(remotes),
            "tags": len(tags),
            "dirty": bool(dirty_any),
        },
        "heads": {
            "latest_commit": latest,
            "branches": list(branches.keys())[:10],
            "remotes": list(remotes.keys())[:10],
            "tags": list(tags.keys())[:10],
        },
    }

from collections import Counter

def _reduce_static(items: List[dict]) -> dict:
    return {
        "family": "static",
        "stats": {
            "count": len(items),
            "by_severity": Counter([i.get("severity","") for i in items]).most_common(),
            "by_check": Counter([i.get("check","") for i in items]).most_common(),
            "by_code": Counter([i.get("code","") for i in items]).most_common(),
        },
        "items": [
            {
                "path": i.get("path"),
                "line": i.get("line"),
                "col": i.get("col"),
                "severity": i.get("severity"),
                "check": i.get("check"),
                "code": i.get("code"),
                "message": i.get("message"),
            } for i in items
        ],
    }





# ──────────────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────────────


#_REDUCERS["static"] = _reduce_static
_REDUCERS: Dict[str, Callable[[List[dict]], dict]] = {
    # Family-specific reducers
    "quality": _reduce_quality,
    "entrypoints": _reduce_entrypoints,
    "env": _reduce_env,
    "deps": _reduce_deps,
    "ast_imports": _reduce_ast_imports,
    "io_core": _reduce_io_core,
    "git": _reduce_git,

    # Others fall back to generic counter
}


def get_reducer(family: str) -> Callable[[List[dict]], dict]:
    """Return a reducer for a family; defaults to a generic counter."""
    fam = canonicalize_family(family)
    return _REDUCERS.get(fam, lambda items: _generic_counter(items, fam))


def zero_summary_for(family: str) -> dict:
    """Produce a non-misleading zero summary for empty families."""
    fam = canonicalize_family(family)
    return {"family": fam, "stats": {"count": 0}, "items": []}







