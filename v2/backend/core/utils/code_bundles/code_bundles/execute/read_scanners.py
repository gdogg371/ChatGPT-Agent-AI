from __future__ import annotations
import time
import inspect
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional, Tuple, Iterable

# Wired scanners
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.doc_coverage import scan as scan_doc_coverage
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.complexity import scan as scan_complexity
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.owners_index import scan as scan_owners
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.env_index import scan as scan_env
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.entrypoints import scan as scan_entrypoints
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.html_index import scan as scan_html
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.sql_index import scan as scan_sql
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.js_ts_index import scan as scan_js_ts
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.deps_scan import scan_dependencies
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.static_check import static_check_scan
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.git_info import scan as scan_git
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.license_scan import scan as scan_license
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.secrets_scan import scan as scan_secrets
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.assets_index import scan as scan_assets

from v2.backend.core.utils.code_bundles.code_bundles.bundle_io import (
    ManifestAppender,
    emit_standard_artifacts,
    emit_transport_parts,
    rewrite_manifest_paths,
    write_sha256sums_for_file,
)
from v2.backend.core.utils.code_bundles.code_bundles.execute.fs import (
    write_sha256sums_for_parts
)
from v2.backend.core.utils.code_bundles.code_bundles.contracts import (
    build_manifest_header,
    build_bundle_summary,
)
from v2.backend.core.utils.code_bundles.code_bundles.graphs import coalesce_edges
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.scanners.python_index import index_python_file
from v2.backend.core.utils.code_bundles.code_bundles.quality import quality_for_python
from v2.backend.core.utils.code_bundles.code_bundles.execute.funcs import (
    map_record_paths_inplace,
    tool_versions
)


def append_records(app: ManifestAppender, records: Optional[Iterable[Dict[str, Any]]], map_path_fn) -> int:
    n = 0
    if not records:
        return 0
    for rec in records:
        if isinstance(rec, dict):
            map_record_paths_inplace(rec, map_path_fn)
            app.append_record(rec)
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

    emitted_prefix = str(cfg.emitted_prefix).strip("/")

    def map_path(rel: str) -> str:
        rel = rel.lstrip("/")
        if path_mode == "github":
            return rel
        return f"{emitted_prefix}/{rel}" if emitted_prefix else rel

    t0 = t1 = t2 = t3 = time.perf_counter()
    module_count = 0
    quality_count = 0
    edges_accum: List[Dict[str, Any]] = []

    ast_symbols = 0
    ast_xrefs = 0
    ast_calls = 0
    ast_docstrings = 0
    ast_symmetrics = 0

    # Python indexing: modules + edges (+ optional AST if provided by indexer)
    for local, rel in discovered_repo:
        if not rel.endswith(".py"):
            continue

        # Optional emit_ast support (backwards compatible with existing signature)
        use_emit_ast = bool(getattr(cfg, "emit_ast", False))
        res = None
        try:
            sig = inspect.signature(index_python_file)
            if "emit_ast" in sig.parameters:
                res = index_python_file(
                    repo_root=Path(cfg.source_root),
                    local_path=local,
                    repo_rel_posix=rel,
                    emit_ast=use_emit_ast,
                )
            else:
                res = index_python_file(
                    repo_root=Path(cfg.source_root),
                    local_path=local,
                    repo_rel_posix=rel,
                )
        except Exception as e:
            # Fallback to legacy call if signature probing failed oddly
            try:
                res = index_python_file(
                    repo_root=Path(cfg.source_root),
                    local_path=local,
                    repo_rel_posix=rel,
                )
            except Exception as e2:
                print(f"[packager] WARN: python_index failed for {rel}: {type(e2).__name__}: {e2}")
                continue

        mod_rec = None
        edges: List[Dict[str, Any]] = []
        extras: Optional[Any] = None

        if isinstance(res, (list, tuple)):
            if len(res) >= 1:
                mod_rec = res[0]
            if len(res) >= 2:
                edges = list(res[1] or [])
            if len(res) >= 3:
                extras = res[2]
        elif isinstance(res, dict):
            # If indexer returns a dict-like
            mod_rec = res.get("module")
            edges = list(res.get("edges") or [])
            extras = res.get("ast")

        # module
        if mod_rec:
            mod_rec["path"] = map_path(rel)
            app.append_record(mod_rec)
            module_count += 1

        # edges
        if edges:
            for e in edges:
                e["src_path"] = map_path(e.get("src_path") or rel)
                if "dst_path" in e and isinstance(e["dst_path"], str):
                    e["dst_path"] = map_path(e["dst_path"])
            edges_accum.extend(edges)

        # optional AST extras (only when cfg.emit_ast is True and indexer returned them)
        if extras and bool(getattr(cfg, "emit_ast", False)):
            # Accept either dict/NS with named lists or flat list of records
            def _take_list(name: str) -> List[Dict[str, Any]]:
                if isinstance(extras, dict):
                    v = extras.get(name)
                else:
                    v = getattr(extras, name, None)
                return list(v or [])

            # If extras is a flat list of records, treat them generically
            if isinstance(extras, (list, tuple)) and all(isinstance(x, dict) for x in extras):
                ast_symbols += append_records(app, extras, map_path)
            else:
                ast_symbols += append_records(app, _take_list("symbols"), map_path)
                ast_xrefs += append_records(app, _take_list("xrefs"), map_path)
                ast_calls += append_records(app, _take_list("calls"), map_path)
                ast_docstrings += append_records(app, _take_list("docstrings"), map_path)
                ast_symmetrics += append_records(app, _take_list("symbol_metrics"), map_path)

    t1 = time.perf_counter()

    # Per-file quality metrics
    for local, rel in discovered_repo:
        if not rel.endswith(".py"):
            continue
        qrec = quality_for_python(path=local, repo_rel_posix=rel)
        qrec["path"] = map_path(rel)
        app.append_record(qrec)
        quality_count += 1

    t2 = time.perf_counter()

    # Coalesce import edges
    edges_dedup = coalesce_edges(edges_accum)
    for e in edges_dedup:
        app.append_record(e)

    t3 = time.perf_counter()

    # Run wired scanners
    def run_scanner(name: str, fn, *args, **kwargs):
        try:
            records = fn(*args, **kwargs) or []
        except Exception as e:
            print(f"[packager] WARN: scanner '{name}' failed: {type(e).__name__}: {e}")
            return 0
        return append_records(app, records, map_path)

    wired_counts: Dict[str, int] = {}
    wired_counts["doc_coverage"] = run_scanner("doc_coverage", scan_doc_coverage, Path(cfg.source_root), discovered_repo)
    wired_counts["complexity"] = run_scanner("complexity", scan_complexity, Path(cfg.source_root), discovered_repo)
    wired_counts["owners"] = run_scanner("owners_index", scan_owners, Path(cfg.source_root), discovered_repo)
    wired_counts["env"] = run_scanner("env_index", scan_env, Path(cfg.source_root), discovered_repo)
    wired_counts["entrypoints"] = run_scanner("entrypoints", scan_entrypoints, Path(cfg.source_root), discovered_repo)
    wired_counts["html"] = run_scanner("html_index", scan_html, Path(cfg.source_root), discovered_repo)
    wired_counts["sql"] = run_scanner("sql_index", scan_sql, Path(cfg.source_root), discovered_repo)
    wired_counts["js_ts"] = run_scanner("js_ts_index", scan_js_ts, Path(cfg.source_root), discovered_repo)
    wired_counts["deps"] = run_scanner(
        "deps",
        lambda root, _repo: scan_dependencies(repo_root=root, cfg=cfg),
        Path(cfg.source_root),
        discovered_repo,
    )
    wired_counts["static_check"] = run_scanner("static_check", static_check_scan, Path(cfg.source_root), discovered_repo)
    wired_counts["git"] = run_scanner("git_info", scan_git, Path(cfg.source_root), discovered_repo)
    wired_counts["license"] = run_scanner("license_scan", scan_license, Path(cfg.source_root), discovered_repo)
    wired_counts["secrets"] = run_scanner("secrets_scan", scan_secrets, Path(cfg.source_root), discovered_repo)
    wired_counts["assets"] = run_scanner("assets_index", scan_assets, Path(cfg.source_root), discovered_repo)

    # Standard artifacts + transport parts emission records
    art_count = 0
    art_count += emit_standard_artifacts(
        appender=app,
        out_bundle=Path(cfg.out_bundle),
        out_sums=Path(cfg.out_sums),
        out_runspec=Path(cfg.out_runspec) if cfg.out_runspec else None,
        out_guide=Path(cfg.out_guide) if cfg.out_guide else None,
    )
    art_count += emit_transport_parts(
        appender=app,
        parts_dir=Path(cfg.out_bundle).parent,
        part_stem=str(cfg.transport.part_stem),
        part_ext=str(cfg.transport.part_ext),
        parts_index_name=str(cfg.transport.parts_index_name),
    )

    # Write SHA256SUMS for chunked manifest (when monolith is not used)
    try:
        write_sha256sums_for_parts(
            parts_dir=Path(cfg.out_bundle).parent,
            parts_index_name=str(cfg.transport.parts_index_name),
            part_stem=str(cfg.transport.part_stem),
            part_ext=str(cfg.transport.part_ext),
            out_sums_path=Path(cfg.out_sums),
        )
    except Exception as e:
        print(f"[packager] WARN: failed to write SHA256")

        # Build summary with AST counts if any
    counts_base = {
        "files": len(discovered_repo),
        "modules": module_count,
        "edges": len(edges_dedup),
        "metrics": quality_count,
        "artifacts": art_count,
        **{f"wired.{k}": v for k, v in wired_counts.items()},
    }
    if bool(getattr(cfg, "emit_ast", False)):
        counts_base.update(
            {
                "ast.symbols": ast_symbols,
                "ast.xrefs": ast_xrefs,
                "ast.calls": ast_calls,
                "ast.docstrings": ast_docstrings,
                "ast.symbol_metrics": ast_symmetrics,
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
    app.append_record(summary)

    print(
        "[packager] Augment manifest: "
        f"modules={module_count}, metrics={quality_count}, edges={len(edges_dedup)}, "
        f"artifacts={art_count}, path_mode={path_mode}, "
        + (
            "ast={symbols:%d, xrefs:%d, calls:%d, docstrings:%d, symmetrics:%d}, "
            % (ast_symbols, ast_xrefs, ast_calls, ast_docstrings, ast_symmetrics)
            if bool(getattr(cfg, 'emit_ast', False))
            else ""
        )
        + "wired={" + ", ".join(f"{k}:{v}" for k, v in wired_counts.items()) + "}"
    )