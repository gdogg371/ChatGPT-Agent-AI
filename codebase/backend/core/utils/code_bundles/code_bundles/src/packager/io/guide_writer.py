from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Any
import json

# Robust import for shared config types
try:
    from packager.core.config import PackConfig, Limits, Policy
except ImportError:
    from ..core.config import PackConfig, Limits, Policy


class GuideWriter:
    """Writes assistant_handoff.v1.json (model orientation + transport instructions)."""

    def __init__(self, out_path: Path) -> None:
        self.out_path = out_path

    def write(
        self,
        reading_order: List[Dict[str, str]],
        cfg: PackConfig,
        prompts_meta: Optional[dict],
        split_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        guide = self.build(reading_order, cfg, prompts_meta, split_info)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        with self.out_path.open("w", encoding="utf-8") as f:
            json.dump(guide, f, ensure_ascii=False, sort_keys=True, indent=2)

    # ---- helpers --------------------------------------------------------------

    @staticmethod
    def _omit_none(d: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def _limits(cls, lim: Optional[Limits]) -> Optional[Dict[str, Any]]:
        if lim is None:
            return None
        d = {
            k: getattr(lim, k)
            for k in (
                "reply_token_budget",
                "max_files_touched",
                "max_diff_size_bytes",
                "reasoning_budget_tokens",
                "max_runs_per_cycle",
            )
        }
        d = cls._omit_none(d)
        return d if d else None

    @staticmethod
    def _constraints(policy: Policy) -> Dict[str, Any]:
        c = policy.sandbox_constraints
        out: Dict[str, Any] = {"offline_only": c.offline_only}
        if c.max_cpu_seconds is not None:
            out["max_cpu_seconds"] = c.max_cpu_seconds
        if c.max_memory_mb is not None:
            out["max_memory_mb"] = c.max_memory_mb
        if c.timeout_seconds_per_run is not None:
            out["timeout_seconds_per_run"] = c.timeout_seconds_per_run
        return out

    @staticmethod
    def _reassembly(manifest_name: str, sums_name: str, split_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build cross-platform reassembly instructions based on split/index metadata.
        """
        parts = list(split_info.get("parts", []))
        t_hint = split_info.get("transport_hint", "txt")
        # Pattern for grouped dirs (e.g., design_manifest_01/design_manifest.part01.txt)
        # We emit a generic glob that matches across groups.
        ext = "txt" if t_hint == "txt" else "jsonl"
        part_glob = f"design_manifest_*/design_manifest.part*.{ext}"

        guidance = {
            "why": "The bundle was split to respect upload/token limits.",
            "expected_output": manifest_name,
            "strategy": "concatenate in lexicographic order",
            "verify_with": sums_name,
            "examples": {
                # POSIX shells
                "bash": f"cat {part_glob} > {manifest_name}",
                "zsh":  f"cat {part_glob} > {manifest_name}",
                # PowerShell (Windows): recurse, sort by name, then concatenate
                "powershell": f'Get-ChildItem -Recurse -File "{part_glob}" | Sort-Object Name | Get-Content | Set-Content -NoNewline {manifest_name}',
                # cmd.exe: best-effort; for stable ordering prefer PowerShell
                "cmd.exe": f'for /R %f in ({part_glob}) do type "%f" >> {manifest_name}',
            },
            "integrity_check": {
                # On POSIX systems having sha256sum:
                "bash": f"sha256sum -c {sums_name} | grep {manifest_name}",
                # On PowerShell:
                "powershell": f"Get-FileHash {manifest_name} -Algorithm SHA256",
                "note": "SHA256SUMS includes the monolithic file entry and part files; the monolith may be omitted on disk if preserve_monolith=false.",
            },
        }

        # If we have an explicit ordered list, include it as authoritative guidance.
        if parts:
            guidance["parts_order"] = parts

        return guidance

    @classmethod
    def build(
        cls,
        reading_order: List[Dict[str, str]],
        cfg: PackConfig,
        prompts_meta: Optional[dict],
        split_info: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        # Core bundle pointers (logical names as emitted by orchestrator)
        manifest_name = cfg.out_bundle.name
        sums_name = cfg.out_sums.name

        # Transport section (robust to optional fields)
        upload_hint = getattr(cfg.transport, "upload_batch_hint", None)
        transport_block: Dict[str, Any] = {
            "format": ("txt" if cfg.transport.transport_as_text else "jsonl"),
            "chunk_records": bool(cfg.transport.chunk_records),
            "chunk_bytes": cfg.transport.chunk_bytes,
            "split_bytes": cfg.transport.split_bytes,
            "grouping": {
                "group_dirs": cfg.transport.group_dirs,
                "parts_per_dir": cfg.transport.parts_per_dir,
                "dir_suffix_width": cfg.transport.dir_suffix_width,
                "dir_pattern": f"{cfg.transport.part_stem}_{{:0{cfg.transport.dir_suffix_width}d}}",
            },
            "parts_index_name": cfg.transport.parts_index_name,
            "part_stem": cfg.transport.part_stem,
            "part_ext": cfg.transport.part_ext,
        }
        if upload_hint is not None:
            transport_block["suggested_batch_size"] = upload_hint

        guide: Dict[str, Any] = {
            "version": "1",
            "purpose": "Enable the assistant to understand and safely modify the project.",
            "reading_order": reading_order,
            "bundle": {
                "manifest": manifest_name,
                "checksums": sums_name,
            },
            "transport": transport_block,
            "execution_policy": {
                "sandbox": {
                    "require_attempt": cfg.policy.execution_policy.require_attempt,
                    "phases": list(cfg.policy.execution_policy.phases),
                    "scope": {
                        "received_snippets": True,
                        "modified_files": True,
                        "entrypoints_if_detected": True,
                    },
                    "constraints": cls._constraints(cfg.policy),
                    "secrets_policy": cfg.policy.secrets_policy,
                    "fallback_if_unavailable": (
                        "If the sandbox is unavailable, perform static analysis and return exact "
                        "reproduction steps with the smallest runnable snippet."
                    ),
                    "observability": {
                        "collect": ["stdout", "stderr", "tracebacks", "exit_code"],
                        "summarize_failures": True,
                    },
                }
            },
            "upload_instructions": [
                "Upload all part files in lexicographic order (grouped directories are expected).",
                "If the UI has a per-batch file limit, upload in batches; keep the order.",
                "After upload, reassemble locally if needed using the commands below and verify with SHA256.",
            ],
        }

        lim = cls._limits(cfg.limits)
        if lim:
            guide["limits"] = lim

        if prompts_meta is not None:
            guide["prompts"] = prompts_meta

        if split_info:
            guide["parts_index"] = split_info.get("index")
            guide["reassembly"] = cls._reassembly(manifest_name, sums_name, split_info)

        return guide
