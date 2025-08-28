# File: v2/backend/core/prompt_pipeline/executor/plugin_api.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, Iterable, Dict, Any, List, Optional, runtime_checkable

@dataclass
class Item:
    """
    Minimal work item handed to a task adapter.

    id:       stable identifier used to correlate model output -> item
    relpath:  repo-relative POSIX path to the file containing the symbol
    lineno:   1-based line number of the target symbol (0 for module)
    source:   optional free-form tag (e.g., table name or provider)
    meta:     adapter-specific metadata (kept opaque to the executor)
    """
    id: str
    relpath: str
    lineno: int
    source: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None


@dataclass
class Prompt:
    """LLM-ready messages."""
    system: str
    user: str


@dataclass
class Result:
    """
    Parsed result for a single item.

    diagnostics: arbitrary structured details the adapter may attach
    new_docstring: None when the model chose not to change anything
    """
    id: str
    diagnostics: Dict[str, Any]
    new_docstring: Optional[str] = None


@runtime_checkable
class TaskAdapter(Protocol):
    """
    Adapter interface for domain-specific prompt building and response parsing.
    Concrete implementations should be side-effect free; mutation should only
    occur inside `apply`.
    """
    task_name: str

    def prepare_items(self, rows: Iterable[Item]) -> List[Item]:
        """
        Transform raw rows (e.g., DB/introspection outputs) into Items
        suitable for prompting. Implementations may enrich `.meta`.
        """
        ...

    def build_prompt(self, batch: List[Item]) -> Prompt:
        """
        Build system+user messages for a batch. Messages MUST be deterministic
        for reproducibility (given the same inputs).
        """
        ...

    def parse_response(self, raw: str) -> Dict[str, Result]:
        """
        Parse the raw LLM response text into a mapping {id -> Result}.
        Adapters should be defensive: tolerate code fences, smart quotes,
        and trailing commas if possible.
        """
        ...

    def verify(self, item: Item, result: Result) -> List[str]:
        """
        Return a list of human-readable issues. Empty list means pass.
        The executor may surface these to a verify step.
        """
        ...

    def apply(self, item: Item, result: Result) -> str:
        """
        Apply the change for `item` (e.g., patch file contents) and return a
        concise summary of the action taken (e.g., "rewrote docstring on line 42").
        """
        ...


__all__ = ["Item", "Prompt", "Result", "TaskAdapter"]

