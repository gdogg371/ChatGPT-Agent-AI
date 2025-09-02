from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Dict, Optional

from v2.backend.core.utils.code_bundles.code_bundles.src.packager.core.writer import write_json_atomic, ensure_dir
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.io.guide_writer import GuideWriter


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _Result:
    out_bundle: Path
    out_runspec: Path
    out_guide: Path


class Packager:
    """
    Minimal orchestrator used by your manage_github.py.
    Responsibilities:
      - Ensure design_manifest directory exists
      - Create placeholder design_manifest.jsonl (if missing)
      - Write superbundle.run.json and assistant_handoff.v1.json
      - Return paths for run_pack to continue with augmentation/chunking/publish
    """

    def __init__(self, cfg: Any, rules: Optional[Any] = None) -> None:
        # cfg is a SimpleNamespace created in run_pack.build_cfg
        self.cfg = cfg
        self.rules = rules

    def run(self, external_source: Optional[Any] = None) -> _Result:
        bundle_path = Path(self.cfg.out_bundle)
        runspec_path = Path(self.cfg.out_runspec)
        guide_path = Path(self.cfg.out_guide)

        # Ensure artifact directory exists
        art_dir = bundle_path.parent
        ensure_dir(art_dir)

        # 1) Make sure the bundle file exists (empty placeholder is fine; run_pack will append)
        if not bundle_path.exists():
            bundle_path.write_text("", encoding="utf-8")

        # 2) Write a compact run-spec (reflecting basic cfg fields)
        mode = str(getattr(self.cfg.publish, "mode", "local")).lower()
        run_spec: Dict[str, Any] = {
            "record_type": "superbundle.run",
            "generated_at": _iso_now(),
            "source_root": str(self.cfg.source_root),
            "modes": {
                "local": mode in {"local", "both"},
                "github": mode in {"github", "both"},
            },
            "transport": {
                "part_stem": str(getattr(self.cfg.transport, "part_stem", "design_manifest")),
                "part_ext": str(getattr(self.cfg.transport, "part_ext", ".txt")),
                "parts_per_dir": int(getattr(self.cfg.transport, "parts_per_dir", 10)),
                "split_bytes": int(getattr(self.cfg.transport, "split_bytes", 150000)),
                "preserve_monolith": bool(getattr(self.cfg.transport, "preserve_monolith", False)),
            },
        }
        write_json_atomic(runspec_path, run_spec)

        # 3) Write a richer assistant handoff using the GuideWriter (single write path)
        try:

            GuideWriter(guide_path).write(cfg=self.cfg)
        except Exception:
            # Fallback: minimal handoff if GuideWriter is unavailable or fails
            handoff: Dict[str, Any] = {
                "record_type": "assistant_handoff.v1",
                "generated_at": _iso_now(),
                "prefer_parts_index": True,
            }
            write_json_atomic(guide_path, handoff)

        return _Result(out_bundle=bundle_path, out_runspec=runspec_path, out_guide=guide_path)


