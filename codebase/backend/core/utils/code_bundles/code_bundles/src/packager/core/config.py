from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


# --------------------------- Prompts -----------------------------------------

@dataclass
class PromptSource:
    """
    A container for optional prompt assets to embed into the bundle.

    kind: one of {"fs_dir","zip","inline"}
      - fs_dir: value is a Path-like directory containing prompt files
      - zip:    value is a Path to a .zip OR raw zip bytes
      - inline: value is a dict[str, str|bytes] mapping relative paths -> content
    """
    kind: str
    value: Any


# --------------------------- Sandbox Policy ----------------------------------

@dataclass
class SandboxConstraints:
    offline_only: bool = True
    max_cpu_seconds: Optional[int] = None
    max_memory_mb: Optional[int] = None
    timeout_seconds_per_run: Optional[int] = None


@dataclass
class ExecutionPolicy:
    """
    require_attempt: if True, the assistant should attempt a sandbox run
    phases: tuple of phase names when runs are expected (e.g., ("on_intake","end_of_dev_cycle"))
    """
    require_attempt: bool = True
    phases: Tuple[str, ...] = ("on_intake", "end_of_dev_cycle")


@dataclass
class Policy:
    sandbox_constraints: SandboxConstraints = field(default_factory=SandboxConstraints)
    execution_policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    # High-level secret handling guidance made visible to the assistant.
    # Kept simple on purpose; expand later if needed.
    secrets_policy: Dict[str, Any] = field(default_factory=lambda: {"no_secrets": True})


# --------------------------- Limits ------------------------------------------

@dataclass
class Limits:
    reply_token_budget: Optional[int] = None
    max_files_touched: Optional[int] = None
    max_diff_size_bytes: Optional[int] = None
    reasoning_budget_tokens: Optional[int] = None
    max_runs_per_cycle: Optional[int] = None


# --------------------------- Transport Options --------------------------------

@dataclass
class TransportOptions:
    """
    Controls how the design manifest is emitted and split for transport/upload.
    """
    # record formatting
    transport_as_text: bool = True         # use .txt parts (still JSONL lines)
    chunk_records: bool = True             # emit 'file_chunk' records for large files
    chunk_bytes: int = 64_000              # bytes per chunk record if chunk_records=True

    # splitting into parts
    split_bytes: int = 300_000             # target bytes per part file
    preserve_monolith: bool = False        # keep the monolithic design_manifest.jsonl

    # filenames/patterns
    part_stem: str = "design_manifest"
    part_ext: str = ".txt"
    parts_index_name: str = "design_manifest_parts_index.json"

    # grouping of parts into subdirectories (e.g., design_manifest_01/, ..._02/)
    group_dirs: bool = True
    parts_per_dir: int = 10
    dir_suffix_width: int = 2  # design_manifest_01, _02, ...

    # Optional UI/UX hint for batch uploads (purely advisory)
    upload_batch_hint: Optional[int] = None


# --------------------------- Publish Options ----------------------------------

@dataclass
class GitHubPublish:
    owner: str = ""
    repo: str = ""
    branch: str = "main"
    base_path: str = ""  # subfolder in the repo; "" means repo root


@dataclass
class PublishOptions:
    """
    Configure where (and what) to publish after building the pack.
      mode: "local" | "github" | "both"
    """
    mode: str = "local"
    github: Optional[GitHubPublish] = None
    github_token: Optional[str] = None
    local_publish_root: Optional[Path] = None

    # What to publish
    publish_codebase: bool = True
    publish_analysis: bool = True
    publish_handoff: bool = True
    publish_transport: bool = False
    publish_prompts: bool = True

    # Optional cleanup (for GitHub and Local publishers)
    clean_before_publish: bool = False

    # Optional filters (not currently used by orchestrator; reserved)
    exclude_globs: Tuple[str, ...] = ()
    secret_patterns: Tuple[str, ...] = ()


# --------------------------- Pack Config --------------------------------------

@dataclass
class PackConfig:
    # Where the external source is mirrored to (ingestion target)
    source_root: Path

    # Outputs
    out_bundle: Path
    out_sums: Path
    out_runspec: Path
    out_guide: Path

    # Emitted logical path prefix inside the bundle (e.g., "codebase/")
    emitted_prefix: str = "codebase/"

    # Discovery controls
    include_globs: Tuple[str, ...] = ()
    exclude_globs: Tuple[str, ...] = ()

    # Segment excludes: any matching directory name at any depth is excluded
    segment_excludes: Tuple[str, ...] = (
        ".git", ".hg", ".svn",
        "__pycache__", ".venv", "venv",
        "node_modules", "dist", "build",
        "output", "software"
    )

    # Optional limits and policy
    limits: Optional[Limits] = None
    policy: Policy = field(default_factory=Policy)

    # Prompt embedding
    prompts: Optional[PromptSource] = None
    prompt_mode: str = "omit"  # "embed" | "omit"

    # FS behavior
    follow_symlinks: bool = False
    case_insensitive: Optional[bool] = None  # if None -> auto-detect by OS

    # Transport + Publish
    transport: TransportOptions = field(default_factory=TransportOptions)
    publish: Optional[PublishOptions] = None

    # Helpers
    def effective_case_insensitive(self) -> bool:
        if self.case_insensitive is not None:
            return self.case_insensitive
        # Windows file systems are generally case-insensitive
        return os.name == "nt"
