from __future__ import annotations

from pathlib import Path
from v2.backend.core.configuration.config import PatchLoopConfig
from v2.backend.core.prompt_pipeline.executor.orchestrator import Orchestrator
from secret_management.secrets_loader import get_secret  # ← add this import

# ...

API_KEY = get_secret("OPENAI_API_KEY", default="")  # looks in secret_management/, then ~/.config/packager/


# --- EDIT THESE DEFAULTS ---
DB_URL        = r"sqlite:///C:/Users/cg371/PycharmProjects/ChatGPT Bot/databases/bot_dev.db"
PROJECT_ROOT  = Path(r"/")
OUT_BASE      = Path(r"C:\Users\cg371\PycharmProjects\ChatGPT Bot\tests_adhoc\patch_loop_test2\output\patches_test")
PROVIDER      = "openai"       # or "mock"
MODEL         = "gpt-4o-mini"  # or "auto"
API_KEY       = get_secret("open_api", default="")  # looks in secret_management/, then ~/.config/packager/
MAX_ROWS      = 14              # set None for all records
VERBOSE       = True
PRESERVE_CRLF = False

# Step toggles (your “control booleans”)
RUN_SCAN_DOCSTRINGS         = False  # step 1
RUN_GET_CODE_FOR_DOCSTRINGS = True   # step 2
RUN_BUILD_PROMPTS           = True   # step 3
RUN_RUN_LLM                 = True   # step 4
RUN_SAVE_PATCH              = True   # step 5
RUN_APPLY_PATCH_SANDBOX     = True   # step 6
RUN_VERIFY_DOCSTRING        = True   # step 7
RUN_ARCHIVE_AND_REPLACE     = False  # step 8 (safety)
RUN_ROLLBACK                = False  # step 9 (safety)
CONFIRM_PROD_WRITES         = False  # guard for steps 8 & 9

# NEW: artifact toggles (match the CLI flags)
SAVE_PER_ITEM_PATCHES = True   # one patch per DB item
SAVE_COMBINED_PATCH   = False   # one combined patch per source file

def main():
    cfg = PatchLoopConfig(
        project_root=PROJECT_ROOT,
        out_base=OUT_BASE,
        run_id_suffix=None,
        sqlalchemy_url=DB_URL,
        sqlalchemy_table="introspection_index",
        status_filter="active",           # set to None to disable status filter
        max_rows=MAX_ROWS,                # <-- “restrict DB records” switch
        model=MODEL,
        provider=PROVIDER,
        api_key=API_KEY,                  # <-- uses this; if None, falls back to hardcoded var in CLI
        run_scan_docstrings=RUN_SCAN_DOCSTRINGS,
        run_get_code_for_docstrings=RUN_GET_CODE_FOR_DOCSTRINGS,
        run_build_prompts=RUN_BUILD_PROMPTS,
        run_run_llm=RUN_RUN_LLM,
        run_save_patch=RUN_SAVE_PATCH,
        run_apply_patch_sandbox=RUN_APPLY_PATCH_SANDBOX,
        run_verify_docstring=RUN_VERIFY_DOCSTRING,
        run_archive_and_replace=RUN_ARCHIVE_AND_REPLACE,
        run_rollback=RUN_ROLLBACK,
        confirm_prod_writes=CONFIRM_PROD_WRITES,
        preserve_crlf=PRESERVE_CRLF,
        verbose=VERBOSE,                  # <-- “verbose mode” switch
    )
    # pass artifact toggles to Orchestrator (no schema change needed)
    cfg.save_per_item_patches = SAVE_PER_ITEM_PATCHES
    cfg.save_combined_patch   = SAVE_COMBINED_PATCH

    Orchestrator(cfg).run()

if __name__ == "__main__":
    main()
