from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Literal

ModelProvider = Literal["mock", "openai"]

@dataclass(slots=True)
class PatchLoopConfig:
    project_root: Path = Path("").resolve()
    out_base: Path = Path("../../../patches/output/patches_received")
    run_id_suffix: Optional[str] = None

    save_per_item_patches: bool = True  # write one patch per DB item
    save_combined_patch: bool = True  # also write a single combined patch per source file

    # DB source
    sqlalchemy_url: str = "sqlite:///./introspection.db"
    sqlalchemy_table: str = "introspection_index"
    status_filter: Optional[str] = "active"
    max_rows: Optional[int] = None

    # LLM
    model: str = "auto"
    provider: ModelProvider = "mock"
    api_key: Optional[str] = None
    max_output_tokens: int = 1024

    # Budgeting
    model_ctx_tokens: int = 128_000
    response_tokens_per_item: int = 300
    budget_guardrail: int = 1024
    batch_overhead_tokens: int = 800  # account for system + schema + delimiters

    # Step toggles (1, 8, 9 default ON per your direction)
    run_scan_docstrings: bool = True           # Step 1
    run_get_code_for_docstrings: bool = True   # Step 2
    run_build_prompts: bool = True             # Step 3
    run_run_llm: bool = True                   # Step 4
    run_save_patch: bool = True                # Step 5
    run_apply_patch_sandbox: bool = True       # Step 6
    run_verify_docstring: bool = True          # Step 7
    run_archive_and_replace: bool = True       # Step 8
    run_rollback: bool = True                  # Step 9

    # Safety gate for prod writes (affects steps 8 & 9)
    confirm_prod_writes: bool = False

    # Newline handling
    preserve_crlf: bool = False  # if True, preserve CRLF per file when original has CRLF

    # Verbose console output (prompt preview)
    verbose: bool = True

    def normalize(self) -> None:
        self.project_root = self.project_root.resolve()
        self.out_base = self.out_base.resolve()
        self.save_per_item_patches = bool(self.save_per_item_patches)
        self.save_combined_patch = bool(self.save_combined_patch)
