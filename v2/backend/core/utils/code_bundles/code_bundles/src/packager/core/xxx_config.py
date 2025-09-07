#v2/backend/core/utils/code_bundles/code_bundles/src/packager/core/xxx_config.py
"""
Packager configuration containers (no hardcoded business defaults).

This change removes embedded path/glob defaults that previously duplicated runtime
configuration, ensuring all effective values come from the caller (e.g. the YAML-
driven runner). Transport/policy/limits keep internal defaults as execution
parameters (not deployment config).

Notes:
- `PackConfig` now requires callers to supply emitted_prefix, include/exclude globs,
  and segment_excludes explicitly. The YAML runner supplies these.
- Existing orchestrator code accesses attributes via `getattr(cfg, ...)`, so
  passing a SimpleNamespace is still compatible (the runner does this).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Literal, Sequence, List, Dict


# -----------------------------
# Transport (manifest splitting)
# -----------------------------

@dataclass
class TransportOptions:
    # Internal execution knobs (not deployment config)
    split_bytes: int = 300_000
    chunk_records: bool = True
    chunk_bytes: int = 64_000
    part_stem: str = "design_manifest"
    part_ext: str = ".txt"
    dir_suffix_width: int = 2
    parts_per_dir: int = 10
    group_dirs: bool = True
    transport_as_text: bool = True
    parts_index_name: str = "design_manifest_parts_index.json"
    preserve_monolith: bool = False


# -----------------------------
# GitHub publishing coordinates
# -----------------------------

@dataclass
class GitHubTarget:
    owner: str
    repo: str
    branch: str = "main"
    base_path: str = ""


# -----------------------------
# Publish configuration
# -----------------------------

PublishMode = Literal["local", "github", "both"]


@dataclass
class PublishOptions:
    mode: PublishMode = "local"
    publish_codebase: bool = True
    publish_analysis: bool = False
    publish_handoff: bool = True
    publish_transport: bool = True
    local_publish_root: Optional[Path] = None
    clean_before_publish: bool = False
    github: Optional[GitHubTarget] = None
    github_token: Optional[str] = None


# -----------------------------
# Legacy-compat shims (for writers)
# -----------------------------

@dataclass
class Limits:
    # Known fields
    max_manifest_bytes: int = 50_000_000
    max_part_bytes: int = 1_000_000
    max_files: int = 5000
    # Common extras some writers reference
    reply_token_budget: Optional[int] = None
    max_reply_tokens: Optional[int] = None

    # Tolerate any unknown attribute a writer may request
    def __getattr__(self, _name: str):
        return None


# ---- Execution Policy (shape expected by guide_writer) ------------------------

@dataclass
class SandboxConstraints:
    """
    Attribute-tolerant constraints container.

    Any attribute not declared will resolve to None (so legacy writers don't crash).
    """
    offline_only: bool = True
    max_cpu_seconds: Optional[int] = None
    max_wall_seconds: Optional[int] = None
    max_memory_mb: Optional[int] = None
    network_access: Optional[bool] = None
    internet_access: Optional[bool] = None
    filesystem_write: Optional[bool] = None
    process_spawn: Optional[bool] = None
    timeout_seconds_per_run: Optional[int] = None
    timeout_seconds_total: Optional[int] = None

    def __getattr__(self, _name: str):
        return None


@dataclass
class SandboxBlock:
    constraints: SandboxConstraints = field(default_factory=SandboxConstraints)
    phases: List[str] = field(default_factory=lambda: ["on_intake", "end_of_dev_cycle"])
    require_attempt: bool = True
    secrets_policy: Dict[str, bool] = field(default_factory=lambda: {"no_secrets": True})


@dataclass
class Policy:
    """Compatibility container matching multiple legacy access patterns."""
    execution_policy: SandboxBlock = field(default_factory=SandboxBlock)

    # Legacy aliases used by some writers
    @property
    def sandbox_constraints(self) -> SandboxConstraints:
        return self.execution_policy.constraints

    @property
    def secrets_policy(self) -> Dict[str, bool]:
        return self.execution_policy.secrets_policy

    @property
    def phases(self) -> List[str]:
        return self.execution_policy.phases


# -----------------------------
# Top-level pack config
# -----------------------------

@dataclass
class PackConfig:
    # Required by caller (no embedded defaults)
    source_root: Path
    out_bundle: Path
    out_sums: Path
    out_runspec: Path
    out_guide: Path

    # Previously hardcoded — now required
    emitted_prefix: str
    include_globs: Sequence[str]
    exclude_globs: Sequence[str]
    segment_excludes: Sequence[str]

    # Behaviour flags (caller-supplied or left absent on purpose)
    follow_symlinks: bool = False
    case_insensitive: bool = False

    # Prompting controls (kept for writer compatibility)
    prompt_mode: Literal["embed", "skip"] = "skip"
    prompts: Optional[object] = None

    # Publishing / transport / writer policy
    publish: PublishOptions = field(default_factory=PublishOptions)
    transport: TransportOptions = field(default_factory=TransportOptions)

    # Writers compatibility (expected by some legacy GuideWriter implementations)
    policy: Policy = field(default_factory=Policy)
    limits: Limits = field(default_factory=Limits)
