# codebase/src/packager/io/runspec_writer.py
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
import json
import time

try:
    from v2.backend.core.utils.code_bundles.code_bundles.src.packager.core.config import PackConfig, TransportOptions
except Exception:
    from ..core.config import PackConfig, TransportOptions  # type: ignore


class RunSpecWriter:
    """Produces a compact, assistant-facing snapshot of a run."""

    def __init__(self, target_path: Path) -> None:
        self.target_path = target_path

    def build_snapshot(
        self,
        cfg: PackConfig,
        provenance: Dict[str, Any],
        prompts_public: Optional[dict] = None,
    ) -> Dict[str, Any]:
        t: TransportOptions = cfg.transport
        # Build a stable 'config_snapshot' similar to the local superbundle.run.json
        snapshot = {
            "config_snapshot": {
                "emitted_prefix": cfg.emitted_prefix,
                "exclude_globs": list(cfg.exclude_globs),
                "include_globs": list(cfg.include_globs),
                "segment_excludes": list(cfg.segment_excludes),
                "execution_policy": {
                    "sandbox": {
                        "constraints": {
                            "offline_only": True
                        },
                        "phases": ["on_intake", "end_of_dev_cycle"],
                        "require_attempt": True,
                        "secrets_policy": {"no_secrets": True},
                    }
                },
                "transport": {
                    "chunk_bytes": t.chunk_bytes,
                    "chunk_records": bool(t.chunk_records),
                    "grouping": {
                        "dir_pattern": f"{t.part_stem}_{{:0{t.dir_suffix_width}d}}",
                        "dir_suffix_width": t.dir_suffix_width,
                        "group_dirs": bool(t.group_dirs),
                        "parts_per_dir": t.parts_per_dir,
                    },
                    "part_ext": t.part_ext,
                    "part_stem": t.part_stem,
                    "parts_index": t.parts_index_name,
                    "payload_format": "jsonl",
                    "preserve_monolith": False,
                    "split_bytes": t.split_bytes,
                    "transport_hint": "txt" if t.transport_as_text else "jsonl",
                },
            },
            "provenance": dict(provenance),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "version": "1",
        }
        if prompts_public:
            snapshot["prompts"] = prompts_public
        return snapshot

    def write(self, snapshot: Dict[str, Any]) -> None:
        self.target_path.parent.mkdir(parents=True, exist_ok=True)
        self.target_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
