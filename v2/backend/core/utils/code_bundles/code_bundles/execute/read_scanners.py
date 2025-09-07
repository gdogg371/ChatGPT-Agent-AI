from __future__ import annotations

import inspect
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple

# Wired scanners
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.python.doc_coverage import (
    scan as scan_doc_coverage,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.python.complexity import (
    scan as scan_complexity,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.general.owners_index import (
    scan as scan_owners,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.general.env_index import (
    scan as scan_env,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.general.entrypoints import (
    scan as scan_entrypoints,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.html.html_index import (
    scan as scan_html,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.sql.sql_index import (
    scan as scan_sql,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.javascript.js_ts_index import (
    scan as scan_js_ts,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.python.deps_scan import (
    scan_dependencies,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.python.static_check import (
    static_check_scan,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.general.git_info import (
    scan as scan_git,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.general.license_scan import (
    scan as scan_license,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.general.secrets_scan import (
    scan as scan_secrets,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.general.assets_index import (
    scan as scan_assets,
)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.python.python_index import (
    index_python_file,
)

from v2.backend.core.utils.code_bundles.code_bundles.quality import quality_for_python
from v2.backend.core.utils.code_bundles.code_bundles.graphs import coalesce_edges
from v2.backend.core.utils.code_bundles.code_bundles.bundle_io import (
    ManifestAppender,
    emit_standard_artifacts,
    emit_transport_parts,
)
from v2.backend.core.utils.code_bundles.code_bundles.execute.fs import (
    write_sha256sums_for_parts,
)
from v2.backend.core.utils.code_bundles.code_bundles.contracts import (
    build_manifest_header,
    build_bundle_summary,
)
from v2.backend.core.utils.code_bundles.code_bundles.execute.funcs import (
    map_record_paths_inplace,
    tool_versions,
)

# Wrapper (YAML-driven; minimal, centralized)
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.core.record_adapter import (
    wrap_record,
    Producer,
    load_wrapper_policy,
)


# --- Memory-only appender for GitHub flavor ---
class MemoryAppender:
    def __init__(self) -> None:
        self._records: List[Dict[str, Any]] = []
        self._have_header = False

    def ensure_header(self, header: dict) -> None:
        if not self._have_header:
            self._records.append(header)
            self._have_header = True

    def append_record(self, rec: dict) -> None:
        self._records.append(rec)

    def to_jsonl_bytes(self) -> bytes:
        return (
            "\n".join(json.dumps(r, ensure_ascii=False) for r in self._records) + "\n"
        ).encode("utf-8")


class RecordAppender(Protocol):
    def append_record(self, record: Dict[str, Any]) -> None: ...


def append_records(
    app: RecordAppender,
    records: Optional[Iterable[Dict[str, Any]]],
    map_path_fn: Callable[[str], str],
    producer: Optional[Producer] = None,
    detected_at: Optional[str] = None,
    policy=None,
) -> int:
    """
    Minimal wrapper+append helper. Backwards compatible with existing 3-arg call sites.
    - Applies path mapping in-place to common path fields.
    - Routes every record through the YAML-driven wrapper (scanner.record.v1).
    - Skips records the policy marks as excluded (returns None).
    """
    n = 0
    if not records:
        return 0
    pol = policy or load_wrapper_policy()
    prod = producer or Producer(name="unknown.producer", version="unknown")
    for rec in records:
        if isinstance(rec, dict):
            map_record_paths_inplace(rec, map_path_fn)
            env = wrap_record(rec, prod, detected_at=detected_at, policy=pol)
            if env is not None:
                app.append_record(env)
                n += 1
    return n


def augment_manifest(
    *,
    cfg: NS,
    discovered_repo: List[Tuple[Path, str]],
    mode_local: bool,
    mode_github: bool,
    path_mode: str,
) -> None:
    app = ManifestAppender(Path(cfg.out_bundle))

    header = build_manifest_header(
        manifest_version="1.0",
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_root=str(cfg.source_root),
        include_globs=list(cfg.include_globs),
        exclude_globs=list(cfg.exclude_globs),
        segment_excludes=list(cfg.segment_excludes),
        case_insensitive=bool(getattr(cfg, "case_insensitive", False)),
        follow_symlinks=bool(getattr(cfg, "follow_symlinks", False)),
        modes={"local": bool(mode_local), "github": bool(mode_github)},
        tool_versions=tool_versions(),
    )
    app.ensure_header(header)

    # Wrapper policy + run timestamp (use header's generated_at)
    policy = load_wrapper_policy()
    run_ts = header.get("generated_at")
    if not isinstance(run_ts, str):
        run_ts = datetime.now(timezone.utc).isoformat()

    def _producer_from_callable(fn) -> Producer:
        mod = getattr(fn, "__module__", "") or "unknown"
        name = getattr(fn, "__name__", "") or "producer"
        return Producer(name=f"{mod}.{name}", version="unknown")

    emitted_prefix = str(cfg.emitted_prefix).strip("/")

    def map_path(rel: str) -> str:
        rel = rel.lstrip("/")
        if path_mode == "github":
            return rel
        return f"{emitted_prefix}/{rel}" if emitted_prefix else rel

    t0 = time.perf_counter()
    module_count = 0
    quality_count = 0
    edges_accum: List[Dict[str, Any]] = []

    ast_symbols = 0
    ast_xrefs = 0
    ast_calls = 0
    ast_docstrings = 0
    ast_symmetrics = 0

    # Python indexing: module records + import edges (+ optional AST extras)
    for local, rel in discovered_repo:
        if not rel.endswith(".py"):
            continue

        want_ast = bool(getattr(cfg, "emit_ast", False))
        try:
            # Preferred signature (keyword-only)
            res = index_python_file(
                repo_root=Path(cfg.source_root),
                local_path=local,
                repo_rel_posix=rel,
                emit_ast=want_ast,
            )
        except TypeError:
            # Fallback: same without 'emit_ast'
            try:
                res = index_python_file(
                    repo_root=Path(cfg.source_root),
                    local_path=local,
                    repo_rel_posix=rel,
                )
            except TypeError:
                # Legacy positional signature
                res = index_python_file(repo_root=Path(cfg.source_root), local_path=local, repo_rel_posix=rel)
        except Exception as e:
            print(f"[packager] WARN: python_index failed for {rel}: {type(e).__name__}: {e}")
            continue

        mod_rec: Optional[Dict[str, Any]] = None
        edges: List[Dict[str, Any]] = []
        extras: Optional[Any] = None

        if isinstance(res, tuple) and len(res) >= 2:
            mod_rec, edges = res[:2]
            if want_ast and len(res) >= 3:
                extras = res[2]
        elif isinstance(res, dict):
            mod_rec = res.get("module")
            edges = list(res.get("edges") or [])
            extras = res.get("ast")

        if mod_rec:
            module_count += append_records(
                app, [mod_rec], map_path, _producer_from_callable(index_python_file), run_ts, policy
            )

        if edges:
            edges_accum.extend(edges)

        if extras and want_ast:
            def _take_list(name: str) -> List[Dict[str, Any]]:
                if isinstance(extras, dict):
                    v = extras.get(name)
                else:
                    v = getattr(extras, name, None)
                return list(v or [])

            if isinstance(extras, (list, tuple)) and all(isinstance(x, dict) for x in extras):
                ast_symbols += append_records(
                    app, extras, map_path, _producer_from_callable(index_python_file), run_ts, policy
                )
            else:
                ast_symbols += append_records(
                    app, _take_list("symbols"), map_path, _producer_from_callable(index_python_file), run_ts, policy
                )
                ast_xrefs += append_records(
                    app, _take_list("xrefs"), map_path, _producer_from_callable(index_python_file), run_ts, policy
                )
                ast_calls += append_records(
                    app, _take_list("calls"), map_path, _producer_from_callable(index_python_file), run_ts, policy
                )
                ast_docstrings += append_records(
                    app, _take_list("docstrings"), map_path, _producer_from_callable(index_python_file), run_ts, policy
                )
                ast_symmetrics += append_records(
                    app, _take_list("symbol_metrics"), map_path, _producer_from_callable(index_python_file), run_ts, policy
                )

    t1 = time.perf_counter()

    # Per-file quality metrics (Python)
    for local, rel in discovered_repo:
        if not rel.endswith(".py"):
            continue
        qrec = quality_for_python(path=local, repo_rel_posix=rel)
        quality_count += append_records(
            app, [qrec], map_path, _producer_from_callable(quality_for_python), run_ts, policy
        )

    t2 = time.perf_counter()

    # Coalesce import edges and append
    edges_dedup = coalesce_edges(edges_accum)
    append_records(app, edges_dedup, map_path, _producer_from_callable(coalesce_edges), run_ts, policy)

    t3 = time.perf_counter()

    # Run wired scanners (all routed through the wrapper)
    def run_scanner(name: str, fn, *args, **kwargs):
        try:
            records = fn(*args, **kwargs) or []
        except Exception as e:
            print(f"[packager] WARN: scanner '{name}' failed: {type(e).__name__}: {e}")
            return 0
        return append_records(app, records, map_path, _producer_from_callable(fn), run_ts, policy)

    wired_counts: Dict[str, int] = {}
    root = Path(cfg.source_root)

    wired_counts["doc_coverage"] = run_scanner("doc_coverage", scan_doc_coverage, root, discovered_repo)
    wired_counts["complexity"] = run_scanner("complexity", scan_complexity, root, discovered_repo)
    wired_counts["owners"] = run_scanner("owners_index", scan_owners, root, discovered_repo)
    wired_counts["env"] = run_scanner("env_index", scan_env, root, discovered_repo)
    wired_counts["entrypoints"] = run_scanner("entrypoints", scan_entrypoints, root, discovered_repo)
    wired_counts["html"] = run_scanner("html_index", scan_html, root, discovered_repo)
    wired_counts["sql"] = run_scanner("sql_index", scan_sql, root, discovered_repo)
    wired_counts["js_ts"] = run_scanner("js_ts_index", scan_js_ts, root, discovered_repo)
    wired_counts["deps"] = run_scanner(
        "deps", lambda repo_root, _repo: scan_dependencies(repo_root=repo_root, cfg=cfg), root, discovered_repo
    )
    wired_counts["static_check"] = run_scanner("static_check", static_check_scan, root, discovered_repo)
    wired_counts["git"] = run_scanner("git_info", scan_git, root, discovered_repo)
    wired_counts["license"] = run_scanner("license_scan", scan_license, root, discovered_repo)
    wired_counts["secrets"] = run_scanner("secrets_scan", scan_secrets, root, discovered_repo)
    wired_counts["assets"] = run_scanner("assets_index", scan_assets, root, discovered_repo)

    # Final summary (record_type=bundle_summary) — intentionally not wrapped
    counts_base = {
        "modules": int(module_count),
        "quality": int(quality_count),
        "edges": int(len(edges_dedup)),
        **{f"wired.{k}": int(v) for k, v in wired_counts.items()},
    }
    if bool(getattr(cfg, "emit_ast", False)):
        counts_base.update(
            {
                "ast.symbols": int(ast_symbols),
                "ast.xrefs": int(ast_xrefs),
                "ast.calls": int(ast_calls),
                "ast.docstrings": int(ast_docstrings),
                "ast.symbol_metrics": int(ast_symmetrics),
            }
        )

    summary = build_bundle_summary(
        counts=counts_base,
        durations_ms={
            "index_ms": int((t1 - t0) * 1000),
            "quality_ms": int((t2 - t1) * 1000),
            "graph_ms": int((t3 - t2) * 1000),
        },
    )
    if isinstance(summary, dict):
        app.append_record(summary)

    # Emit standard artifacts and transport parts per existing flow
    emit_standard_artifacts(
        appender=app,
        out_bundle=Path(cfg.out_bundle),
        out_sums=Path(cfg.out_sums),
        out_runspec=Path(cfg.out_runspec) if getattr(cfg, "out_runspec", None) else None,
        out_guide=Path(cfg.out_guide) if getattr(cfg, "out_guide", None) else None,
    )

    emit_transport_parts(
        appender=app,
        parts_dir=Path(cfg.out_bundle).parent,
        part_stem=str(cfg.transport.part_stem),
        part_ext=str(cfg.transport.part_ext),
        parts_index_name=str(cfg.transport.parts_index_name),
    )

    write_sha256sums_for_parts(
        parts_dir=Path(cfg.out_bundle).parent,
        parts_index_name=str(cfg.transport.parts_index_name),
        part_stem=str(cfg.transport.part_stem),
        part_ext=str(cfg.transport.part_ext),
        out_sums_path=Path(cfg.out_sums),
    )


def augment_manifest_memory(
    *,
    cfg: NS,
    discovered_repo: List[Tuple[Path, str]],
    mode_local: bool,
    mode_github: bool,
    path_mode: str,   # must be "github" for the memory publisher
) -> bytes:
    """
    In-memory equivalent of augment_manifest:
    - Same records (modules, edges, quality, scanners, AST, summary)
    - Skips on-disk artifact emissions and SHA256SUMS (those are handled by the memory publisher)
    - Returns JSONL bytes (one record per line)
    """
    app = MemoryAppender()

    header = build_manifest_header(
        manifest_version="1.0",
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_root=str(cfg.source_root),
        include_globs=list(cfg.include_globs),
        exclude_globs=list(cfg.exclude_globs),
        segment_excludes=list(cfg.segment_excludes),
        case_insensitive=bool(getattr(cfg, "case_insensitive", False)),
        follow_symlinks=bool(getattr(cfg, "follow_symlinks", False)),
        modes={"local": bool(mode_local), "github": bool(mode_github)},
        tool_versions=tool_versions(),
    )
    app.ensure_header(header)

    policy = load_wrapper_policy()
    run_ts = header.get("generated_at")
    if not isinstance(run_ts, str):
        run_ts = datetime.now(timezone.utc).isoformat()

    def _producer_from_callable(fn) -> Producer:
        mod = getattr(fn, "__module__", "") or "unknown"
        name = getattr(fn, "__name__", "") or "producer"
        return Producer(name=f"{mod}.{name}", version="unknown")

    def map_path(rel: str) -> str:
        # In memory mode, paths are already repo-relative POSIX for GitHub usage
        return rel.lstrip("/")

    t0 = time.perf_counter()
    module_count = 0
    quality_count = 0
    edges_accum: List[Dict[str, Any]] = []

    ast_symbols = 0
    ast_xrefs = 0
    ast_calls = 0
    ast_docstrings = 0
    ast_symmetrics = 0

    for local, rel in discovered_repo:
        if not rel.endswith(".py"):
            continue

        want_ast = bool(getattr(cfg, "emit_ast", False))
        try:
            # Preferred signature (keyword-only)
            res = index_python_file(
                repo_root=Path(cfg.source_root),
                local_path=local,
                repo_rel_posix=rel,
                emit_ast=want_ast,
            )
        except TypeError:
            try:
                res = index_python_file(
                    repo_root=Path(cfg.source_root),
                    local_path=local,
                    repo_rel_posix=rel,
                )
            except TypeError:
                res = index_python_file(repo_root=Path(cfg.source_root), local_path=local, repo_rel_posix=rel)
        except Exception as e:
            print(f"[packager] WARN: python_index failed for {rel}: {type(e).__name__}: {e}")
            continue

        mod_rec: Optional[Dict[str, Any]] = None
        edges: List[Dict[str, Any]] = []
        extras: Optional[Any] = None

        if isinstance(res, tuple) and len(res) >= 2:
            mod_rec, edges = res[:2]
            if want_ast and len(res) >= 3:
                extras = res[2]
        elif isinstance(res, dict):
            mod_rec = res.get("module")
            edges = list(res.get("edges") or [])
            extras = res.get("ast")

        if mod_rec:
            module_count += append_records(
                app, [mod_rec], map_path, _producer_from_callable(index_python_file), run_ts, policy
            )

        if edges:
            edges_accum.extend(edges)

        if extras and want_ast:
            def _take_list(name: str) -> List[Dict[str, Any]]:
                if isinstance(extras, dict):
                    v = extras.get(name)
                else:
                    v = getattr(extras, name, None)
                return list(v or [])

            if isinstance(extras, (list, tuple)) and all(isinstance(x, dict) for x in extras):
                ast_symbols += append_records(
                    app, extras, map_path, _producer_from_callable(index_python_file), run_ts, policy
                )
            else:
                ast_symbols += append_records(
                    app, _take_list("symbols"), map_path, _producer_from_callable(index_python_file), run_ts, policy
                )
                ast_xrefs += append_records(
                    app, _take_list("xrefs"), map_path, _producer_from_callable(index_python_file), run_ts, policy
                )
                ast_calls += append_records(
                    app, _take_list("calls"), map_path, _producer_from_callable(index_python_file), run_ts, policy
                )
                ast_docstrings += append_records(
                    app, _take_list("docstrings"), map_path, _producer_from_callable(index_python_file), run_ts, policy
                )
                ast_symmetrics += append_records(
                    app, _take_list("symbol_metrics"), map_path, _producer_from_callable(index_python_file), run_ts, policy
                )

    t1 = time.perf_counter()

    for local, rel in discovered_repo:
        if not rel.endswith(".py"):
            continue
        qrec = quality_for_python(path=local, repo_rel_posix=rel)
        quality_count += append_records(
            app, [qrec], map_path, _producer_from_callable(quality_for_python), run_ts, policy
        )

    t2 = time.perf_counter()

    edges_dedup = coalesce_edges(edges_accum)
    append_records(app, edges_dedup, map_path, _producer_from_callable(coalesce_edges), run_ts, policy)

    t3 = time.perf_counter()

    def run_scanner(name: str, fn, *args, **kwargs):
        try:
            records = fn(*args, **kwargs) or []
        except Exception as e:
            print(f"[packager] WARN: scanner '{name}' failed: {type(e).__name__}: {e}")
            return 0
        return append_records(app, records, map_path, _producer_from_callable(fn), run_ts, policy)

    wired_counts: Dict[str, int] = {}
    root = Path(cfg.source_root)

    wired_counts["doc_coverage"] = run_scanner("doc_coverage", scan_doc_coverage, root, discovered_repo)
    wired_counts["complexity"] = run_scanner("complexity", scan_complexity, root, discovered_repo)
    wired_counts["owners"] = run_scanner("owners_index", scan_owners, root, discovered_repo)
    wired_counts["env"] = run_scanner("env_index", scan_env, root, discovered_repo)
    wired_counts["entrypoints"] = run_scanner("entrypoints", scan_entrypoints, root, discovered_repo)
    wired_counts["html"] = run_scanner("html_index", scan_html, root, discovered_repo)
    wired_counts["sql"] = run_scanner("sql_index", scan_sql, root, discovered_repo)
    wired_counts["js_ts"] = run_scanner("js_ts_index", scan_js_ts, root, discovered_repo)
    wired_counts["deps"] = run_scanner(
        "deps", lambda repo_root, _repo: scan_dependencies(repo_root=repo_root, cfg=cfg), root, discovered_repo
    )
    wired_counts["static_check"] = run_scanner("static_check", static_check_scan, root, discovered_repo)
    wired_counts["git"] = run_scanner("git_info", scan_git, root, discovered_repo)
    wired_counts["license"] = run_scanner("license_scan", scan_license, root, discovered_repo)
    wired_counts["secrets"] = run_scanner("secrets_scan", scan_secrets, root, discovered_repo)
    wired_counts["assets"] = run_scanner("assets_index", scan_assets, root, discovered_repo)

    counts_base = {
        "modules": int(module_count),
        "quality": int(quality_count),
        "edges": int(len(edges_dedup)),
        **{f"wired.{k}": int(v) for k, v in wired_counts.items()},
    }
    if bool(getattr(cfg, "emit_ast", False)):
        counts_base.update(
            {
                "ast.symbols": int(ast_symbols),
                "ast.xrefs": int(ast_xrefs),
                "ast.calls": int(ast_calls),
                "ast.docstrings": int(ast_docstrings),
                "ast.symbol_metrics": int(ast_symmetrics),
            }
        )

    summary = build_bundle_summary(
        counts=counts_base,
        durations_ms={
            "index_ms": int((t1 - t0) * 1000),
            "quality_ms": int((t2 - t1) * 1000),
            "graph_ms": int((t3 - t2) * 1000),
        },
    )
    if isinstance(summary, dict):
        app.append_record(summary)

    return app.to_jsonl_bytes()

