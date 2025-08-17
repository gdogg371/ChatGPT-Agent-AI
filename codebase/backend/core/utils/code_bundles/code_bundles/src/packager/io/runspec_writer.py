# src/packager/io/runspec_writer.py
from __future__ import annotations
from pathlib import Path
from typing import Optional, Dict, Any
import json, time
from ..core.paths import PathOps
from ..core.config import PackConfig, Limits, Policy

class RunSpecWriter:
    def __init__(self, out_path: Path) -> None:
        self.out_path = out_path

    def write(self, spec: Dict[str, Any]) -> None:
        PathOps.ensure_dir(self.out_path)
        with open(self.out_path, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False, sort_keys=True, indent=2)

    @staticmethod
    def _omit_none(d: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def _limits(cls, lim: Optional[Limits]) -> Optional[Dict[str, Any]]:
        if lim is None: return None
        d = {k: getattr(lim, k) for k in ("reply_token_budget","max_files_touched","max_diff_size_bytes","reasoning_budget_tokens","max_runs_per_cycle")}
        d = cls._omit_none(d)
        return d if d else None

    @staticmethod
    def _constraints(policy: Policy) -> Dict[str, Any]:
        c = policy.sandbox_constraints
        out: Dict[str, Any] = {"offline_only": c.offline_only}
        if c.max_cpu_seconds is not None: out["max_cpu_seconds"] = c.max_cpu_seconds
        if c.max_memory_mb is not None: out["max_memory_mb"] = c.max_memory_mb
        if c.timeout_seconds_per_run is not None: out["timeout_seconds_per_run"] = c.timeout_seconds_per_run
        return out

    @classmethod
    def build_snapshot(cls, cfg: PackConfig, provenance: Dict[str, Any], prompts_meta: Optional[dict]) -> Dict[str, Any]:
        snap: Dict[str, Any] = {
            "version": "1",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "config_snapshot": {
                "emitted_prefix": cfg.emitted_prefix,
                "segment_excludes": list(cfg.segment_excludes),
                "include_globs": list(cfg.include_globs),
                "exclude_globs": list(cfg.exclude_globs),
                "execution_policy": {
                    "sandbox": {
                        "require_attempt": cfg.policy.execution_policy.require_attempt,
                        "phases": list(cfg.policy.execution_policy.phases),
                        "constraints": cls._constraints(cfg.policy),
                        "secrets_policy": cfg.policy.secrets_policy
                    }
                },
                "transport": {
                    "payload_format": "jsonl",
                    "transport_hint": ("txt" if cfg.transport.transport_as_text else "jsonl"),
                    "parts_index": cfg.transport.parts_index_name,
                    "part_stem": cfg.transport.part_stem,
                    "part_ext": (cfg.transport.part_ext if cfg.transport.transport_as_text else ".jsonl"),
                    "chunk_records": bool(cfg.transport.chunk_records),
                    "chunk_bytes": cfg.transport.chunk_bytes,
                    "split_bytes": cfg.transport.split_bytes,
                    "preserve_monolith": cfg.transport.preserve_monolith,
                    "grouping": {
                        "group_dirs": cfg.transport.group_dirs,
                        "parts_per_dir": cfg.transport.parts_per_dir,
                        "dir_suffix_width": cfg.transport.dir_suffix_width,
                        "dir_pattern": f"{cfg.transport.part_stem}_{{:0{cfg.transport.dir_suffix_width}d}}"
                    }
                },
                "prompts": prompts_meta
            },
            "provenance": provenance
        }
        if prompts_meta is None:
            snap["config_snapshot"].pop("prompts")
        return snap
