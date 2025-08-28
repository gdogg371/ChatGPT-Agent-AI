from __future__ import annotations
from typing import Any, Dict, List, Tuple

from v2.backend.core.tasks.base import TaskAdapter
from v2.backend.core.docstrings.sanitize import sanitize_docstring
from v2.backend.core.docstrings.verify import DocstringVerifier
from v2.patches.core.rewrite import apply_docstring_update
from v2.backend.core.prompt_pipeline.executor.steps import (
    BuildContextStep,
    UnpackResultsStep,
)


class DocstringsAdapter(TaskAdapter):
    """
    Task adapter that encapsulates all docstring-specific semantics:
      - item building (delegates to BuildContextStep)
      - response parsing (delegates to UnpackResultsStep)
      - payload sanitation (sanitize_docstring)
      - application to source (apply_docstring_update)
      - verification (PEP257 + params/signature)
    """

    name: str = "docstrings"
    response_format_name: str | None = "docstrings.v1"

    def __init__(self) -> None:
        self._verifier = DocstringVerifier()
        self._builder = BuildContextStep()

    # ---- Item building -------------------------------------------------------
    def build_items(self, suspects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Reuse existing context builder which enriches with signature/lineno, etc.
        return self._builder.run(suspects)

    # ---- Core hooks ----------------------------------------------------------
    def sanitize(self, payload: Any, item: Dict[str, Any]) -> str:
        """
        Expect payload as either a plain string or a dict with key 'docstring'.
        Use the symbol signature from the item for formatting.
        """
        if isinstance(payload, dict):
            text = payload.get("docstring", "")
        else:
            text = str(payload) if payload is not None else ""
        signature = item.get("signature")
        return sanitize_docstring(text, signature=signature)

    def apply(self, original_src: str, item: Dict[str, Any], payload: str) -> str:
        """Insert/replace the docstring at the target lineno for the item."""
        target_lineno = int(item.get("target_lineno") or 0)
        relpath = item.get("relpath") or ""
        return apply_docstring_update(original_src, target_lineno, payload, relpath=relpath)

    def verify(self, payload: str, item: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Run docstring checks: minimal PEP257 + parameter consistency vs signature."""
        signature = item.get("signature")
        ok1, issues1 = self._verifier.pep257_minimal(payload)
        ok2, issues2 = self._verifier.params_consistency(payload, signature)
        ok = bool(ok1 and ok2)
        issues = list(issues1) + list(issues2)
        return ok, issues

    # ---- Parsing -------------------------------------------------------------
    def parse_response(self, raw: str, expected_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Use existing UnpackResultsStep to parse and validate the model output
        against the expected_ids for this batch, then adapt to the generic
        adapter result shape: [{"id": str, "payload": {...}}].
        """
        unpacked = UnpackResultsStep(expected_ids=expected_ids).run(raw)
        results: List[Dict[str, Any]] = []
        for r in unpacked:
            rid = str(r.get("id"))
            payload = {
                "docstring": r.get("docstring", ""),
                "mode": r.get("mode", "rewrite"),
            }
            results.append({"id": rid, "payload": payload})
        return results

