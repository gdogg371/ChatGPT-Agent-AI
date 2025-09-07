# src/packager/io/runspec_writer.py
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime, timezone
import json


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _posix_rel(base: Path, target: Path) -> str:
    base = base.resolve()
    target = target.resolve()
    try:
        return target.relative_to(base).as_posix()
    except Exception:
        return target.as_posix()


def _log(msg: str) -> None:
    print(f"[packager] {msg}", flush=True)


def _as_path(p: Any) -> Path:
    return p if isinstance(p, Path) else Path(str(p))


def _serialize_transport(t: Any) -> Dict[str, Any]:
    """Safely serialize transport-like object (SimpleNamespace or similar)."""
    if not t:
        return {}
    out: Dict[str, Any] = {}
    for k in (
        "chunk_bytes",
        "chunk_records",
        "group_dirs",
        "dir_suffix_width",
        "parts_per_dir",
        "part_ext",
        "part_stem",
        "parts_index_name",
        "split_bytes",
        "transport_as_text",
        "preserve_monolith",
    ):
        if hasattr(t, k):
            out[k] = getattr(t, k)
    return out


class RunSpecWriter:
    def __init__(self, out_path: Path) -> None:
        self.out_path = _as_path(out_path)

    def write(self, runspec: Dict[str, Any]) -> Path:
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(runspec, ensure_ascii=False, sort_keys=True, indent=2)
        self.out_path.write_text(data, encoding="utf-8")
        return self.out_path

    @staticmethod
    def build_snapshot(a: Any, b: Any, prompts_public: Optional[dict] = None) -> Dict[str, Any]:
        """
        Accepts EITHER order:
          - build_snapshot(cfg, meta, prompts)
          - build_snapshot(meta, cfg, prompts)

        Where:
          cfg  ~ object with .source_root, .emitted_prefix, .transport, .out_bundle/out_runspec/out_guide/out_sums
          meta ~ dict like {"source_root": "...", "emitted_prefix": "..."}

        Returns a dict run-spec.
        """
        # Heuristics to disambiguate arguments
        a_is_cfg = hasattr(a, "source_root") or hasattr(a, "transport") or hasattr(a, "out_bundle")
        b_is_cfg = hasattr(b, "source_root") or hasattr(b, "transport") or hasattr(b, "out_bundle")

        if a_is_cfg and not b_is_cfg:
            cfg, meta = a, b
        elif b_is_cfg and not a_is_cfg:
            cfg, meta = b, a
        else:
            # Fallback: treat 'a' as cfg if it quacks like one; else raise a clear error
            if a_is_cfg:
                cfg, meta = a, (b or {})
            elif b_is_cfg:
                cfg, meta = b, (a or {})
            else:
                raise TypeError("RunSpecWriter.build_snapshot: cannot determine cfg/meta argument order")

        # Build provenance (prefer explicit meta)
        provenance: Dict[str, Any] = {}
        if isinstance(meta, dict):
            provenance.update(meta)

        # Fill missing provenance from cfg if available
        if not provenance.get("source_root") and hasattr(cfg, "source_root"):
            provenance["source_root"] = str(cfg.source_root)
        if not provenance.get("emitted_prefix") and hasattr(cfg, "emitted_prefix"):
            provenance["emitted_prefix"] = getattr(cfg, "emitted_prefix")

        # Optional provenance details from cfg (non-breaking)
        if hasattr(cfg, "packager_version") or hasattr(cfg, "version"):
            provenance.setdefault(
                "packager_version",
                getattr(cfg, "packager_version", None) or getattr(cfg, "version", None),
            )
        if any(hasattr(cfg, attr) for attr in ("packager_git_sha", "repo_sha", "git_sha")):
            provenance.setdefault(
                "code_sha",
                getattr(cfg, "packager_git_sha", None)
                or getattr(cfg, "repo_sha", None)
                or getattr(cfg, "git_sha", None),
            )

        # Transport (safe serialization)
        transport = _serialize_transport(getattr(cfg, "transport", None))

        # Paths block (optional, helpful)
        paths: Dict[str, Any] = {}
        if hasattr(cfg, "out_bundle"):
            paths["bundle"] = _as_path(cfg.out_bundle).name
        if hasattr(cfg, "out_runspec"):
            paths["runspec"] = _as_path(cfg.out_runspec).name
        if hasattr(cfg, "out_guide"):
            paths["guide"] = _as_path(cfg.out_guide).name
        if hasattr(cfg, "out_sums"):
            paths["sums"] = _as_path(cfg.out_sums).name

        # Additional fields: artifact_root (relative), filters and fs flags (non-breaking)
        rel_artifact_root: Optional[str] = None
        try:
            if hasattr(cfg, "out_bundle") and provenance.get("source_root"):
                rel_artifact_root = _posix_rel(Path(provenance["source_root"]), _as_path(cfg.out_bundle).parent)
        except Exception:
            rel_artifact_root = None

        include_globs = list(getattr(cfg, "include_globs", []) or [])
        exclude_globs = list(getattr(cfg, "exclude_globs", []) or [])
        segment_excludes = list(getattr(cfg, "segment_excludes", []) or [])

        follow_symlinks = bool(getattr(cfg, "follow_symlinks", True))
        case_insensitive = bool(getattr(cfg, "case_insensitive", True))

        runspec: Dict[str, Any] = {
            "version": "1",
            "generated_at": _iso_now(),  # timestamp (non-breaking)
            "provenance": provenance,
            "prompts": prompts_public or {},
        }

        if rel_artifact_root:
            runspec["artifact_root"] = rel_artifact_root  # relative to source_root

        if include_globs or exclude_globs or segment_excludes:
            runspec["filters"] = {
                "include_globs": include_globs,
                "exclude_globs": exclude_globs,
                "segment_excludes": segment_excludes,
            }

        runspec["fs"] = {
            "follow_symlinks": follow_symlinks,
            "case_insensitive": case_insensitive,
        }

        if transport:
            runspec["transport"] = transport
        if paths:
            runspec["paths"] = paths

        return runspec
