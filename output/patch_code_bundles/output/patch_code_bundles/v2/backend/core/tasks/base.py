from __future__ import annotations
from typing import Protocol, Tuple, List, Dict, Any


class TaskAdapter(Protocol):
    """
    A thin interface that lets the pipeline remain task-agnostic.

    Implementations (e.g., DocstringsAdapter) encapsulate task-specific:
      - item shaping / context building
      - LLM response parsing
      - payload sanitation
      - application of the payload to source text
      - verification of the payload against task rules
    """

    # Human-readable name for logging
    name: str

    # Optional named response format (e.g., "docstrings.v1")
    response_format_name: str | None

    # ---- Core hooks ----------------------------------------------------------
    def sanitize(self, payload: Any, item: Dict[str, Any]) -> str:
        """
        Normalize/clean payload coming back from the LLM so it's safe and
        deterministic for application and verification. Returns a string payload
        suitable for apply().
        """
        ...

    def apply(self, original_src: str, item: Dict[str, Any], payload: str) -> str:
        """
        Apply the sanitized payload to the given source text, returning the
        updated text. Implementations must be pure (no I/O) and deterministic.
        """
        ...

    def verify(self, payload: str, item: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        Check the payload against task-specific rules. Returns (ok, issues).
        Implementations should be side-effect free.
        """
        ...

    # ---- Optional convenience hooks -----------------------------------------
    def build_items(self, suspects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert DB 'suspects' into task-ready 'items' for prompt building."""
        return suspects  # default passthrough

    def parse_response(self, raw: str, expected_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Convert a raw LLM response into a list of results:
        [{ "id": str, "payload": Any, ... }]
        """
        raise NotImplementedError("parse_response is not implemented by this adapter.")
