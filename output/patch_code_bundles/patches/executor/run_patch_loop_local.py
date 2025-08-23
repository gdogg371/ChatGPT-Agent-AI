# File: v2/patches/executor/run_patch_loop_local.py
#!/usr/bin/env python3
from __future__ import annotations

"""
Local, PyCharm-friendly entrypoint for the patch loop — now routed via the Spine.

Flow (configurable by flags):
  1) Build PatchLoopConfig-style payload from CLI
  2) Ask Spine to run the LLM pipeline (capability: llm.engine.run.v1)  [--only-apply to skip]
  3) Gather *.patch files (from run dir and known drop-zones)
  4) Ask Spine to apply patches (capability: patch.apply_files.v1)

No DB schema changes. No Git usage for patch application.
"""

import argparse
import importlib.util
import os
import re
import sys
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Iterable

# --- Spine bus -----------------------------------------------------------------
from v2.backend.core.spine import Spine

# --- Existing LLM pipeline types (for payload shaping) -------------------------
from v2.backend.core.configuration.config import PatchLoopConfig
from v2.backend.core.prompt_pipeline.executor.errors import AskSpecError
from v2.backend.core.types.types import AskSpec


# -------------------- Helpers (OpenAI key resolution) --------------------------

def _find_repo_root(start: Path) -> Path:
    """Walk up from 'start' to filesystem root and return the first directory that contains 'databases/bot_dev.db'. If none is found, return CWD."""
    start = start.resolve()
    for p in [start, *start.parents]:
        if (p / "databases" / "bot_dev.db").is_file():
            return p
    return Path.cwd().resolve()


def _default_project_root() -> Path:
    """Derive a sensible default project root for local IDE runs. Preference: a directory that actually contains the databases/ folder."""
    here = Path(__file__).resolve()
    repo_root = _find_repo_root(here)
    if repo_root:
        return repo_root
    for p in [here.parent, *here.parents]:
        if (p / "backend").is_dir() or (p / "v2" / "backend").is_dir():
            return p
    return Path.cwd().resolve()


def _extract_openai_key(secrets: Dict[str, Any]) -> Optional[str]:
    """Best-effort extraction of an OpenAI API key from a secrets dict."""
    if not isinstance(secrets, dict):
        return None
    # direct env-style
    for k in ("OPENAI_API_KEY", "openai_api_key"):
        val = secrets.get(k)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # nested common shapes
    sec = secrets.get("openai")
    if isinstance(sec, dict):
        val = sec.get("api_key") or sec.get("key")
        if isinstance(val, str) and val.strip():
            return val.strip()
    llm = secrets.get("llm")
    if isinstance(llm, dict):
        sec = llm.get("openai")
        if isinstance(sec, dict):
            val = sec.get("api_key") or sec.get("key")
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _load_openai_api_key_from_loader(repo_root: Path) -> Optional[str]:
    """Try secret_management/secrets_loader.py via common entrypoints."""
    secrets_dir = repo_root / "secret_management"
    loader_path = secrets_dir / "secrets_loader.py"
    yaml_path = secrets_dir / "secrets.yaml"
    if not loader_path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("secrets_loader_local", str(loader_path))
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
    except Exception:
        return None

    candidates = ("load_secrets", "get_secrets", "load", "read")
    for fn_name in candidates:
        fn = getattr(module, fn_name, None)
        if callable(fn):
            # Try with YAML path, fallback to no-arg
            for call in ((yaml_path,), tuple()):
                try:
                    secrets = fn(*call)
                except TypeError:
                    continue
                except Exception:
                    secrets = None
                if secrets:
                    key = _extract_openai_key(secrets)  # <-- correct helper
                    if key:
                        return key
    # Fallback: module-level dict
    secrets_obj = getattr(module, "SECRETS", None)
    if isinstance(secrets_obj, dict):
        key = _extract_openai_key(secrets_obj)  # <-- fixed here
        if key:
            return key
    return None


def _load_openai_api_key_from_yaml(repo_root: Path) -> Optional[str]:
    """Directly read secret_management/secrets.yaml. Uses PyYAML if present, else a tiny parser."""
    yaml_path = repo_root / "secret_management" / "secrets.yaml"
    if not yaml_path.is_file():
        return None
    # Try PyYAML first
    try:
        import yaml  # type: ignore
        with yaml_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return _extract_openai_key(data or {})
    except Exception:
        # Minimal fallback parser for simple structures:
        # openai:\n  api_key: "value"
        try:
            key: Optional[str] = None
            in_openai = False
            with yaml_path.open("r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith(("#", ";")):
                        continue
                    if re.match(r"^openai\s*:\s*$", line):
                        in_openai = True
                        continue
                    if in_openai:
                        m = re.match(r"^api_key\s*:\s*(.+)$", line)
                        if m:
                            val = m.group(1).strip().strip('"').strip("'")
                            if val:
                                key = val
                                break
                    # allow top-level OPENAI_API_KEY: "..."
                    m2 = re.match(r"^OPENAI_API_KEY\s*:\s*(.+)$", line)
                    if m2:
                        val = m2.group(1).strip().strip('"').strip("'")
                        if val:
                            key = val
                            break
            return key
        except Exception:
            return None


def _resolve_openai_api_key(repo_root: Path, cli_key: Optional[str]) -> Optional[str]:
    """Resolve the OpenAI API key using CLI/env/loader/YAML in that order."""
    if cli_key and cli_key.strip():
        return cli_key.strip()
    env_key = os.getenv("OPENAI_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    key = _load_openai_api_key_from_loader(repo_root)
    if key:
        return key
    key = _load_openai_api_key_from_yaml(repo_root)
    if key:
        return key
    return None


def _build_ask_spec(
    profile: str,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_output_tokens: Optional[int] = None,
) -> AskSpec:
    """Map a human-friendly profile to an AskSpec and apply optional overrides."""
    prof = (profile or "").strip().lower()
    if prof in ("doc", "docstrings", "docstrings.v1"):
        spec = AskSpec.for_docstrings()
    elif prof in ("qa", "qa.default"):
        spec = AskSpec.for_qa()
    else:
        raise AskSpecError(f"Unknown ask profile: {profile!r}")

    if model is not None and model != "auto":
        spec.model = model
    if temperature is not None:
        spec.temperature = float(temperature)
    if max_output_tokens is not None:
        spec.max_output_tokens = int(max_output_tokens)

    # Optional: override response format via env (advanced)
    rf_name = os.getenv("LLM_RESPONSE_FORMAT_NAME")
    if rf_name:
        spec.response_format_name = rf_name

    spec.validate()
    return spec


# -------------------- Patch gathering helpers ----------------------------------

RECEIVED_DIR = Path("output/patches_received")
PATCH_SEARCH_ROOTS: List[Path] = [
    RECEIVED_DIR,                # preferred landing zone
    Path("output/patches_test"), # common previous location
    Path("output"),              # broad fallback
]


def _find_patches(roots: Iterable[Path]) -> List[Path]:
    patches: List[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".patch":
            patches.append(root.resolve())
            continue
        if root.is_dir():
            patches.extend(sorted(root.rglob("*.patch")))
    # De-dup by path
    seen = set()
    uniq: List[Path] = []
    for p in patches:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq


def _ensure_in_received(patch_paths: List[Path]) -> List[Path]:
    RECEIVED_DIR.mkdir(parents=True, exist_ok=True)
    final_paths: List[Path] = []
    for p in patch_paths:
        dest = RECEIVED_DIR / p.name
        try:
            if p.resolve() != dest.resolve():
                shutil.copy2(p, dest)
                final_paths.append(dest)
            else:
                final_paths.append(p)
        except Exception:
            # If copy fails, keep original
            final_paths.append(p)
    return final_paths


def _detect_seed_dir(project_root: Path) -> Path:
    """Pick a reasonable default inscope seed dir for the mirror, under project_root."""
    candidates = [
        project_root / "v2" / "backend",
        project_root / "backend",
        project_root / "src",
        project_root,
    ]
    for c in candidates:
        try:
            if (c.exists() and any(c.rglob("*.py"))) or c == project_root:
                return c.resolve()
        except Exception:
            continue
    return project_root.resolve()


# -------------------- Controller ------------------------------------------------

class PatchLoopLocalController:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.project_root: Path = args.project_root.resolve()
        self.out_base: Path = args.out_base.resolve()
        # Spine instance (required; no backwards fallback here)
        caps_path = args.caps_path.resolve() if args.caps_path else (
            self.project_root / "v2" / "backend" / "core" / "spine" / "capabilities.yml"
        )
        self.spine = Spine(caps_path=caps_path)

    # ---- Build the engine payload from CLI flags (maps stage controls) ----
    def build_engine_payload(self) -> Dict[str, Any]:
        extra_cfg: Dict[str, Any] = {}
        if self.args.scan_exclude:
            extra_cfg["scan_exclude_globs"] = tuple(self.args.scan_exclude)

        # Stage switches (set to False when --no-xxx passed)
        stage_overrides: Dict[str, Any] = dict(
            run_fetch_targets=not self.args.no_fetch,
            run_build_prompts=not self.args.no_build,
            run_run_llm=not self.args.no_llm,
            run_save_patch=not self.args.no_save,
            run_apply_patch_sandbox=not self.args.no_sandbox,
            run_verify=not self.args.no_verify,
            run_archive_and_replace=not self.args.no_archive,
            run_rollback=not self.args.no_rollback,
            # run_scan is a no-op placeholder in Engine; keep default True
        )

        payload = dict(
            project_root=str(self.project_root),
            out_base=str(self.out_base),
            provider=self.args.provider,
            model=self.args.model,
            api_key=self.args.api_key_resolved,  # resolved earlier
            sqlalchemy_url=self.args.db_url,
            sqlalchemy_table=self.args.table,
            status_filter=self.args.status,
            max_rows=self.args.max_rows,
            verbose=bool(self.args.verbose),
            confirm_prod_writes=bool(self.args.confirm_prod_writes),
            ask_spec=self.args.ask_spec.to_dict(),  # type: ignore[attr-defined]
            scan_root=str(self.args.scan_root) if self.args.scan_root else "v2",
            **stage_overrides,
            **extra_cfg,
        )
        return payload

    # ---- Run the LLM pipeline via spine ----
    def run_engine(self, payload: Dict[str, Any]) -> Path:
        arts = self.spine.dispatch_capability(
            capability="llm.engine.run.v1",
            payload=payload,
            intent="pipeline",
            subject=str(self.project_root),
            context={"cli": "run_patch_loop_local"},
        )
        # Expect an artifact with meta["result"]["run_dir"]
        run_dir = None
        for a in arts:
            if isinstance(a.meta, dict):
                res = a.meta.get("result")
                if isinstance(res, dict) and res.get("run_dir"):
                    run_dir = res["run_dir"]
                    break
        return Path(run_dir or self.out_base).resolve()

    # ---- Collect patch files from known locations ----
    def gather_patches(self, run_dir: Path) -> List[Path]:
        # Prefer anything the engine wrote under run_dir
        candidates: List[Path] = []
        if run_dir and run_dir.exists():
            candidates.extend(sorted(run_dir.rglob("*.patch")))
        # Also look in the usual drop-zones
        found = _find_patches(PATCH_SEARCH_ROOTS)
        # Merge & de-dup
        seen: set[Path] = set()
        uniq: List[Path] = []
        for p in [*candidates, *found]:
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                uniq.append(p)
        # Normalize to output/patches_received
        return _ensure_in_received(uniq)

    # ---- Apply patches via spine ----
    def apply_patches(self, patches: List[Path]) -> int:
        if not patches:
            print("[patch-engine] No .patch files found. Drop patches into output/patches_received/ and re-run.")
            return 0

        mirror_current = (self.project_root / "output" / "mirrors" / "current").resolve()
        payload = dict(
            patches=[str(p) for p in patches],
            mirror_current=str(mirror_current),
            source_seed_dir=str(_detect_seed_dir(self.project_root)),
            initial_tests=[
                # keep fast & safe; extend as needed
                'python - << "import sys; print(sys.version.split()[0])"',
            ],
            extensive_tests=[
                # project-specific tests can be added here
            ],
            excludes=["**/.git/**", "**/.venv/**", "**/__pycache__/**", "**/output/**"],
            promotion_enabled=False,
        )

        arts = self.spine.dispatch_capability(
            capability="patch.apply_files.v1",
            payload=payload,
            intent="patch",
            subject=str(mirror_current),
            context={"cli": "run_patch_loop_local"},
        )

        # Summarize failures from artifacts
        failures = 0
        for a in arts:
            if a.kind == "Problem":
                failures += 1
            elif isinstance(a.meta, dict):
                status = (a.meta.get("outcome") or {}).get("status")
                if status and status not in ("promoted", "would_promote_but_disabled", "extensive_tests_failed", "initial_tests_failed"):
                    failures += 1
        return 1 if failures else 0

    # ---- Orchestrate all stages based on flags ----
    def run(self) -> int:
        # 1) Build payload for the Engine
        payload = self.build_engine_payload()

        # 2) Optionally run DB->LLM pipeline (skip if user asked to only-apply)
        run_dir = Path("")
        if not self.args.only_apply:
            run_dir = self.run_engine(payload)
            print(f"[run_patch_loop_local] LLM pipeline complete. Artifacts at: {run_dir}")

        # 3) Gather and 4) Apply patches
        patches = self.gather_patches(run_dir)
        print(f"[patch-engine] Will apply {len(patches)} patch file(s) from {RECEIVED_DIR}")
        return self.apply_patches(patches)


# -------------------- Main ------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the LLM patch loop (local) via Spine and apply patches.")

    default_root = _default_project_root()

    parser.add_argument(
        "--project-root",
        type=Path,
        default=default_root,
        help="Project root directory (defaults to detected repo root or CWD).",
    )
    parser.add_argument(
        "--out-base",
        type=Path,
        default=default_root / "output" / "patches_test",
        help="Base directory for run artifacts (LLM pipeline).",
    )
    parser.add_argument(
        "--provider",
        choices=("openai", "mock"),
        default=os.getenv("LLM_PROVIDER", "openai"),
        help="Model provider.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=os.getenv("LLM_MODEL", "auto"),
        help="Model name (or 'auto' to let the router decide).",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.getenv("OPENAI_API_KEY"),
        help="API key.",
    )
    parser.add_argument(
        "--db-url",
        type=str,
        default=os.getenv("INTROSPECTION_DB_URL"),
        help=("SQLAlchemy URL for introspection DB. If omitted, the config layer may resolve a default bot_dev.db"),
    )
    parser.add_argument(
        "--table",
        type=str,
        default=os.getenv("INTROSPECTION_TABLE", "introspection_index"),
        help="Table name for introspection rows.",
    )
    parser.add_argument(
        "--status",
        type=str,
        default="active",
        help="Optional status filter (e.g., 'active').",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Limit number of rows processed.",
    )
    parser.add_argument(
        "--ask-profile",
        type=str,
        default="docstrings",
        help="Ask profile: 'docstrings' or 'qa'.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Override AskSpec.temperature (0.0–2.0).",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help="Override AskSpec.max_output_tokens.",
    )
    parser.add_argument(
        "--scan-root",
        type=str,
        default=os.getenv("SCAN_ROOT", "v2"),
        help="Repo-relative directory to constrain scanning (e.g., 'v2').",
    )
    parser.add_argument(
        "--scan-exclude",
        type=str,
        action="append",
        default=None,
        help=("Glob pattern to exclude from scanning (repeatable). Defaults already exclude 'output/**', '.git/**', '__pycache__/**', '.venv/**', 'node_modules/**'."),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=True,
        help="Verbose logging.",
    )
    parser.add_argument(
        "--confirm-prod-writes",
        action="store_true",
        default=False,
        help="Allow archive+replace and rollback steps to write to source files.",
    )
    parser.add_argument(
        "--only-apply",
        action="store_true",
        default=False,
        help="Skip the LLM pipeline and only apply patches found in known locations.",
    )
    # Stage toggles
    parser.add_argument("--no-fetch", action="store_true", help="Skip reading targets from DB")
    parser.add_argument("--no-build", action="store_true", help="Skip building prompts/packs")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM calls")
    parser.add_argument("--no-save", action="store_true", help="Skip writing patches to disk")
    parser.add_argument("--no-sandbox", action="store_true", help="Skip sandbox apply")
    parser.add_argument("--no-verify", action="store_true", help="Skip verification stage")
    parser.add_argument("--no-archive", action="store_true", help="Skip archive & replace stage")
    parser.add_argument("--no-rollback", action="store_true", help="Skip rollback placeholder stage")

    # Spine config
    parser.add_argument(
        "--caps-path",
        type=Path,
        default=None,
        help="Path to backend/core/spine/capabilities.yml (defaults to repo conventional path).",
    )

    args = parser.parse_args(argv)

    # AskSpec and API key resolution
    try:
        args.ask_spec = _build_ask_spec(
            profile=args.ask_profile,
            model=args.model,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens,
        )
    except AskSpecError as e:
        print(f"[run_patch_loop_local] {e}", file=sys.stderr)
        return 2

    args.api_key_resolved = _resolve_openai_api_key(args.project_root, args.api_key)
    if args.provider == "openai" and not args.api_key_resolved:
        project_root = args.project_root.resolve()
        secrets_yaml = project_root / "secret_management" / "secrets.yaml"
        print(
            "[run_patch_loop_local] Missing OpenAI API key.\n"
            "Tried: --api-key, env OPENAI_API_KEY, secrets_loader.py, secrets.yaml\n"
            f"Checked YAML path: {secrets_yaml}\n"
            "Please set one of the above and retry.",
            file=sys.stderr,
        )
        return 2

    controller = PatchLoopLocalController(args)
    return controller.run()


if __name__ == "__main__":
    raise SystemExit(main())

