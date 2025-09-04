# src/packager/io/runspec_writer.py
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
import json

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

        runspec: Dict[str, Any] = {
            "version": "1",
            "provenance": provenance,
            "prompts": prompts_public or {},
        }

        if transport:
            runspec["transport"] = transport
        if paths:
            runspec["paths"] = paths

        return runspec
