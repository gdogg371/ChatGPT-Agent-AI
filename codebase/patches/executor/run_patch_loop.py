from __future__ import annotations

from pathlib import Path
import typer

# Prefer absolute imports here to avoid "no parent package" issues
from v2.backend.core.configuration.config import PatchLoopConfig
from v2.backend.core.prompt_pipeline.executor.orchestrator import Orchestrator

# Per your instruction: explicit hardcoded key (no envs/key vaults yet)
HARDCODED_OPENAI_API_KEY: str | None = None  # <-- place a key here if you want a default

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command("run")
def run(
    project_root: Path = typer.Option(Path(""), help="Project root for resolving filepaths"),
    out_base: Path = typer.Option(Path("../output/patches_received"), help="Base output dir"),
    run_id_suffix: str = typer.Option(None, help="Optional suffix for run id"),
    db_url: str = typer.Option(..., "--db-url", help="SQLAlchemy URL for introspection DB (required)"),
    table: str = typer.Option("introspection_index", help="Table name"),
    status: str = typer.Option("active", help="Filter by status; pass '' to disable"),
    max_rows: int = typer.Option(None, help="Limit number of rows from DB"),
    model: str = typer.Option("auto", help="Model name or 'auto'"),
    provider: str = typer.Option("mock", help="LLM provider: mock or openai"),
    api_key: str = typer.Option(None, help="API key if provider='openai'"),
    # Step toggles
    run_scan_docstrings: bool = typer.Option(True, help="Step 1: scan source for docstrings (external tool)"),
    run_get_code_for_docstrings: bool = typer.Option(True, help="Step 2"),
    run_build_prompts: bool = typer.Option(True, help="Step 3"),
    run_run_llm: bool = typer.Option(True, help="Step 4"),
    run_save_patch: bool = typer.Option(True, help="Step 5"),
    run_apply_patch_sandbox: bool = typer.Option(True, help="Step 6"),
    run_verify_docstring: bool = typer.Option(True, help="Step 7"),
    run_archive_and_replace: bool = typer.Option(True, help="Step 8"),
    run_rollback: bool = typer.Option(True, help="Step 9"),
    # Safety confirmation for prod writes (steps 8 & 9)
    confirm_prod_writes: bool = typer.Option(False, help="REQUIRED to perform archive/replace and rollback operations"),
    # Newline handling
    preserve_crlf: bool = typer.Option(False, help="Preserve CRLF on write when the original file used CRLF"),
    verbose: bool = typer.Option(True, help="Verbose console output (prompt preview)"),
    # NEW: artifact toggles
    save_per_item_patches: bool = typer.Option(
        True,
        help="Write individual patch files per DB item (good for selective apply/audit).",
    ),
    save_combined_patch: bool = typer.Option(
        True,
        help="Also write a single combined patch per source file (good for final apply).",
    ),
):
    cfg = PatchLoopConfig(
        project_root=Path(project_root),
        out_base=Path(out_base),
        run_id_suffix=run_id_suffix,
        sqlalchemy_url=db_url,
        sqlalchemy_table=table,
        status_filter=(status if status else None),
        max_rows=max_rows,
        model=model,
        provider=provider,
        api_key=(api_key or HARDCODED_OPENAI_API_KEY),
        run_scan_docstrings=run_scan_docstrings,
        run_get_code_for_docstrings=run_get_code_for_docstrings,
        run_build_prompts=run_build_prompts,
        run_run_llm=run_run_llm,
        run_save_patch=run_save_patch,
        run_apply_patch_sandbox=run_apply_patch_sandbox,
        run_verify_docstring=run_verify_docstring,
        run_archive_and_replace=run_archive_and_replace,
        run_rollback=run_rollback,
        confirm_prod_writes=confirm_prod_writes,
        preserve_crlf=preserve_crlf,
        verbose=verbose,
    )
    # Orchestrator reads these via getattr with defaults; set explicitly from CLI:
    cfg.save_per_item_patches = save_per_item_patches
    cfg.save_combined_patch = save_combined_patch

    root = Orchestrator(cfg).run()
    typer.echo(str(root))


if __name__ == "__main__":
    app()
