# File: v2/backend/core/prompt_pipeline/executor/engine.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import AskSpecError, ValidationError
from .steps import (
    BuildContextStep,
    PackPromptStep,
    UnpackResultsStep,   # canonical unpacker
)
from .prompts import (
    build_system_prompt,
    build_user_prompt,
    build_packed,
)
from .retriever import SourceRetriever
from .sources import IntrospectionDbSource

from ..llm.client import LlmClient

from ...docstrings.sanitize import sanitize_rows
from ...docstrings.verify import verify_rows

# Spine contracts for capability wrappers
from v2.backend.core.spine.contracts import Artifact, Task


# ---------------------------------------------------------------------------
# Simple configuration object for the Engine
# ---------------------------------------------------------------------------

@dataclass
class EngineConfig:
    provider: str = "openai"     # Literal["mock","openai"] in calling sites, but tolerant here
    model: str = "gpt-4o-mini"   # router/client can override if needed
    max_rows: int = 200

    # DB fetch config (used by BuildContextStep/SourceRetriever)
    sqlalchemy_url: Optional[str] = None
    sqlalchemy_table: str = "introspection_index"
    status_filter: str = "active"

    # Exclusion globs for the *pipeline* side (doc scanner uses dir basenames)
    exclude_globs: Optional[List[str]] = None

    # Stage toggles (pipeline YAML can mirror these; we also honor them directly)
    run_fetch_targets: bool = True
    run_build_prompts: bool = True
    run_run_llm: bool = True
    run_unpack: bool = True
    run_sanitize: bool = True
    run_verify: bool = True
    run_save_patch: bool = True            # prepare patch artifacts, do not apply
    run_apply_patch_sandbox: bool = False  # explicitly off here (caller may enable elsewhere)
    run_archive_and_replace: bool = False
    run_rollback: bool = False

    # Output wiring (the writer/apply stages use these downstream; we only pass through)
    out_base: str = "output/patches_test"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    """
    Parameterised pipeline runner for the docstring patch loop:
      fetch -> build prompts -> run LLM -> unpack -> sanitize -> verify -> (save patch only)
    """

    def __init__(self, cfg: EngineConfig):
        self.cfg = cfg
        self.client = LlmClient(provider=cfg.provider, model=cfg.model)

    # --------------------- Stage: fetch targets -------------------------------

    def _fetch_targets(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Use the Introspection DB as the source of targets. Returns (items, meta).
        Each item must include an 'id' and any context required to build prompts.
        """
        src = IntrospectionDbSource(
            sqlalchemy_url=self.cfg.sqlalchemy_url,
            table=self.cfg.sqlalchemy_table,
            status_filter=self.cfg.status_filter,
            limit=self.cfg.max_rows,
        )
        retriever = SourceRetriever(source=src, exclude_globs=self.cfg.exclude_globs or [])
        items, meta = retriever.collect()
        if not items:
            raise ValidationError("No targets found for docstring patching.")
        return items, meta

    # --------------------- Stage: pack prompts --------------------------------

    def _build_prompts(
        self, items: List[Dict[str, Any]], ask_spec: Dict[str, Any]
    ) -> Tuple[str, str, List[str]]:
        """
        Build (system, user, expected_ids). We keep both granular (system/user) and packed forms
        to remain compatible with routes that expect either.
        """
        system = build_system_prompt(ask_spec, self.cfg)
        user = build_user_prompt(items, ask_spec, self.cfg)

        # expected ids: these are checked during unpack/validation
        expected_ids = [str(it["id"]) for it in items if "id" in it]
        if not expected_ids:
            raise ValidationError("No 'id' fields found in items for prompt building.")
        return system, user, expected_ids

    # --------------------- Stage: run LLM -------------------------------------

    def _run_llm(self, system: str, user: str) -> Dict[str, Any]:
        """
        Execute the request via the configured LLM client.
        Accepts either:
          - an object with attributes: .text (str), optional .usage
          - a dict with keys: {"text": str, "usage": ...}
        Returns a dict: {"text": str, "usage": Any}
        """
        result = self.client.complete(system=system, user=user)

        # object with attributes
        if hasattr(result, "text"):
            text = getattr(result, "text", None)
            usage = getattr(result, "usage", None)
        # dict-like
        elif isinstance(result, dict):
            text = result.get("text")
            usage = result.get("usage")
        else:
            raise ValidationError("LLM returned unsupported result type")

        if not isinstance(text, str) or not text.strip():
            raise ValidationError("LLM response was empty.")

        return {"text": text, "usage": usage}

    # --------------------- Stage: unpack --------------------------------------

    def _unpack(self, raw_text: str, expected_ids: Iterable[str]) -> List[Dict[str, Any]]:
        """
        Single, canonical place to interpret LLM results.
        Uses UnpackResultsStep from executor.steps.
        """
        step = UnpackResultsStep(expected_ids=list(expected_ids))
        rows = step.run(raw_text)  # [{"id": "...", "mode": "...", "docstring": "..."}, ...]
        if not rows:
            raise ValidationError("Unpack produced no rows.")
        return rows

    # --------------------- Stage: sanitize ------------------------------------

    def _sanitize(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        clean = sanitize_rows(rows)
        if not clean:
            raise ValidationError("All rows were filtered out during sanitize.")
        return clean

    # --------------------- Stage: verify --------------------------------------

    def _verify(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        ok, reports = verify_rows(rows)
        if not ok:
            bad = [r for r in reports if not r.get("ok")]
            raise ValidationError(
                f"Verification failed for {len(bad)} items: "
                + ", ".join(r["id"] or "?" for r in bad[:5])
            )
        return rows

    # --------------------- Public API -----------------------------------------

    def run(self, *, ask_spec: Dict[str, Any]) -> Dict[str, Any]:
        """
        Runs the configured stages. Returns a dict with artifacts & telemetry; the actual
        “save/apply” of patches is intentionally out-of-scope here (promotion is disabled).
        """
        cfg = self.cfg
        artifacts: Dict[str, Any] = {
            "out_base": cfg.out_base,
            "provider": cfg.provider,
            "model": cfg.model,
        }

        # 1) Fetch
        if cfg.run_fetch_targets:
            items, meta = self._fetch_targets()
            artifacts["fetched"] = len(items)
            artifacts["fetch_meta"] = meta
        else:
            raise AskSpecError("run_fetch_targets=False requires caller-provided items.")

        # 2) Build prompts
        if cfg.run_build_prompts:
            system, user, expected_ids = self._build_prompts(items, ask_spec)
            artifacts["expected_ids"] = expected_ids
        else:
            raise AskSpecError("run_build_prompts=False is not supported in this Engine.")

        # 3) Call LLM
        if cfg.run_run_llm:
            llm_result = self._run_llm(system, user)
            artifacts["llm_tokens"] = llm_result.get("usage")
            raw_text = llm_result["text"]
        else:
            raise AskSpecError("run_run_llm=False requires caller-provided raw_text.")

        # 4) Unpack
        if cfg.run_unpack:
            rows = self._unpack(raw_text, artifacts["expected_ids"])
            artifacts["unpacked"] = len(rows)
        else:
            raise AskSpecError("run_unpack=False requires caller-provided parsed rows.")

        # 5) Sanitize
        if cfg.run_sanitize:
            rows = self._sanitize(rows)
            artifacts["sanitized"] = len(rows)

        # 6) Verify
        if cfg.run_verify:
            rows = self._verify(rows)
            artifacts["verified"] = len(rows)

        # 7) Save patch (prepare only; applying/promoting is out of scope here)
        if cfg.run_save_patch:
            artifacts["patch_rows"] = rows

        # Explicitly refuse apply/promote in this Engine variant
        if cfg.run_apply_patch_sandbox or cfg.run_archive_and_replace or cfg.run_rollback:
            raise AskSpecError("Apply/Archive/Rollback are disabled in Engine; use patch_engine stages.")

        artifacts["status"] = "ok"
        return artifacts


# ---------------------------------------------------------------------------
# Spine capability wrapper: llm.engine.run.v1 → engine.run_v1
# ---------------------------------------------------------------------------

def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return bool(v)


def _result(uri: str, meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri=uri, sha256="", meta=meta)]


def _problem(uri: str, code: str, message: str) -> List[Artifact]:
    return [
        Artifact(
            kind="Problem",
            uri=uri,
            sha256="",
            meta={"problem": {"code": code, "message": message, "retryable": False, "details": {}}},
        )
    ]


def run_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Capability target for: llm.engine.run.v1
    Loads config from task.payload, runs Engine, and returns a Result artifact.
    """
    p = task.payload or {}

    if not _bool(p.get("run", True)):
        return _result("spine://result/llm.engine.run.v1", {"status": "skipped"})

    try:
        cfg = EngineConfig(
            provider=str(p.get("provider") or "openai"),
            model=str(p.get("model") or "gpt-4o-mini"),
            max_rows=int(p.get("max_rows") or 200),
            sqlalchemy_url=p.get("sqlalchemy_url"),
            sqlalchemy_table=str(p.get("sqlalchemy_table") or "introspection_index"),
            status_filter=str(p.get("status_filter") or "active"),
            exclude_globs=list(p.get("exclude_globs") or []),
            run_fetch_targets=_bool(p.get("run_fetch_targets", True)),
            run_build_prompts=_bool(p.get("run_build_prompts", True)),
            run_run_llm=_bool(p.get("run_run_llm", True)),
            run_unpack=_bool(p.get("run_unpack", True)),
            run_sanitize=_bool(p.get("run_sanitize", True)),
            run_verify=_bool(p.get("run_verify", True)),
            run_save_patch=_bool(p.get("run_save_patch", True)),
            run_apply_patch_sandbox=_bool(p.get("run_apply_patch_sandbox", False)),
            run_archive_and_replace=_bool(p.get("run_archive_and_replace", False)),
            run_rollback=_bool(p.get("run_rollback", False)),
            out_base=str(p.get("out_base") or "output/patches_test"),
        )

        ask_spec = dict(p.get("ask_spec") or {})
        art = Engine(cfg).run(ask_spec=ask_spec)
        return _result("spine://result/llm.engine.run.v1", art)

    except AskSpecError as e:
        return _problem("spine://problem/llm.engine.run.v1", "InvalidPayload", str(e))
    except ValidationError as e:
        return _problem("spine://problem/llm.engine.run.v1", "ValidationError", str(e))
    except Exception as e:
        return _problem("spine://problem/llm.engine.run.v1", "UnhandledError", f"{type(e).__name__}: {e}")


__all__ = ["EngineConfig", "Engine", "run_v1"]





