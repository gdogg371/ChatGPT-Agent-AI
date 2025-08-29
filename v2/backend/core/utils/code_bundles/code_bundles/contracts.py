# File: v2/backend/core/utils/code_bundles/code_bundles/contracts.py
"""
Record builders for design_manifest.jsonl enrichment.

We keep the existing 'file' records produced by the Packager untouched,
and add additional record kinds to enrich the manifest:

- manifest.header
- python.module
- graph.edge
- quality.metric
- artifact
- bundle.summary

All builders return plain dicts ready to be JSONL-serialized.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


# ──────────────────────────────────────────────────────────────────────────────
# Dataclasses (only for clarity; we emit dicts)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ManifestHeader:
    kind: str
    manifest_version: str
    generated_at: str
    source_root: str
    include_globs: List[str]
    exclude_globs: List[str]
    segment_excludes: List[str]
    case_insensitive: bool
    follow_symlinks: bool
    modes: Dict[str, bool]
    tool_versions: Dict[str, str]


@dataclass
class PythonModule:
    kind: str
    path: str                   # repo-relative posix path to the .py file
    module: str                 # dotted module path
    package_root: str           # repo-relative package root (module dir)
    has_init: bool              # whether package has __init__.py at module dir
    imports: List[Dict[str, Any]]  # structured imports
    defs: Dict[str, List[Dict[str, Any]]]  # {'classes': [...], 'functions': [...]}
    doc: Optional[str] = None   # first line of module docstring (optional)


@dataclass
class GraphEdge:
    kind: str
    src_path: str               # repo-relative posix path (file that imports)
    dst_module: str             # dotted module name being imported
    edge_type: str = "import"   # 'import' | 'from' | other future kinds


@dataclass
class QualityMetric:
    kind: str
    path: str                   # repo-relative posix path
    language: str               # e.g., 'python'
    sloc: int                   # source lines of code (non-blank, non-comment)
    loc: int                    # total line count
    cyclomatic: int             # simple cyclomatic complexity estimate
    n_functions: int
    n_classes: int
    avg_fn_len: float
    notes: Optional[List[str]] = None


@dataclass
class ArtifactRec:
    kind: str
    name: str                   # e.g., 'design_manifest.jsonl'
    path: str                   # output path where artifact was written
    kind_hint: str              # 'manifest' | 'sums' | 'run_spec' | 'guide' | 'transport_part' | 'transport_index'


@dataclass
class BundleSummary:
    kind: str
    counts: Dict[str, int]      # e.g., {'files': 123, 'modules': 100, 'edges': 456, 'metrics': 100, 'artifacts': 4}
    durations_ms: Dict[str, int]  # e.g., {'index_ms': 120, 'quality_ms': 80, 'graph_ms': 5}


# ──────────────────────────────────────────────────────────────────────────────
# Builders
# ──────────────────────────────────────────────────────────────────────────────

def build_manifest_header(
    *,
    manifest_version: str,
    generated_at: str,
    source_root: str,
    include_globs: List[str],
    exclude_globs: List[str],
    segment_excludes: List[str],
    case_insensitive: bool,
    follow_symlinks: bool,
    modes: Dict[str, bool],
    tool_versions: Dict[str, str],
) -> Dict[str, Any]:
    rec = ManifestHeader(
        kind="manifest.header",
        manifest_version=manifest_version,
        generated_at=generated_at,
        source_root=source_root,
        include_globs=include_globs,
        exclude_globs=exclude_globs,
        segment_excludes=segment_excludes,
        case_insensitive=case_insensitive,
        follow_symlinks=follow_symlinks,
        modes=modes,
        tool_versions=tool_versions,
    )
    return asdict(rec)


def build_python_module(
    *,
    path: str,
    module: str,
    package_root: str,
    has_init: bool,
    imports: List[Dict[str, Any]],
    classes: List[Dict[str, Any]],
    functions: List[Dict[str, Any]],
    doc: Optional[str],
) -> Dict[str, Any]:
    rec = PythonModule(
        kind="python.module",
        path=path,
        module=module,
        package_root=package_root,
        has_init=has_init,
        imports=imports,
        defs={"classes": classes, "functions": functions},
        doc=doc,
    )
    return asdict(rec)


def build_graph_edge(
    *,
    src_path: str,
    dst_module: str,
    edge_type: str = "import",
) -> Dict[str, Any]:
    rec = GraphEdge(
        kind="graph.edge",
        src_path=src_path,
        dst_module=dst_module,
        edge_type=edge_type or "import",
    )
    return asdict(rec)


def build_quality_metric(
    *,
    path: str,
    language: str,
    sloc: int,
    loc: int,
    cyclomatic: int,
    n_functions: int,
    n_classes: int,
    avg_fn_len: float,
    notes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    rec = QualityMetric(
        kind="quality.metric",
        path=path,
        language=language,
        sloc=int(sloc),
        loc=int(loc),
        cyclomatic=int(cyclomatic),
        n_functions=int(n_functions),
        n_classes=int(n_classes),
        avg_fn_len=float(avg_fn_len),
        notes=notes or None,
    )
    return asdict(rec)


def build_artifact(
    *,
    name: str,
    path: str,
    kind_hint: str,
) -> Dict[str, Any]:
    rec = ArtifactRec(
        kind="artifact",
        name=name,
        path=path,
        kind_hint=kind_hint,
    )
    return asdict(rec)


def build_bundle_summary(
    *,
    counts: Dict[str, int],
    durations_ms: Dict[str, int],
) -> Dict[str, Any]:
    rec = BundleSummary(
        kind="bundle.summary",
        counts=counts,
        durations_ms=durations_ms,
    )
    return asdict(rec)
