from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, TypedDict, NotRequired


# ---------------------------------------------------------------------------
# Existing shapes (preserved)
# ---------------------------------------------------------------------------

class DbRow(TypedDict):
    id: int
    filepath: str
    symbol_type: str
    name: str | None
    lineno: int | None
    unique_key_hash: str | None
    description: str | None


class Suspect(TypedDict):
    id: str                 # canonical stable id (unique_key_hash or computed)
    path: str               # absolute path
    relpath: str            # path relative to project root
    lineno: int             # hint line from DB (may be 0/None)
    target_lineno: int      # AST-detected exact node lineno (module=1)
    symbol_type: str        # module | class | function | route | ...
    signature: NotRequired[str]
    has_docstring: NotRequired[bool]
    context: NotRequired[str]


# ---------------------------------------------------------------------------
# NEW: Pipeline ask parameterisation
# ---------------------------------------------------------------------------

class AskType(str, Enum):
    """
    High-level intent categories for LLM interactions.

    - code_ops: touches or reasons about code (refactor, docstrings, debugging,
      extending, remediation). This class of asks may enable codebase-context.
    - qa: general question answering that does not require repo context or
      JSON-schema-constrained outputs.
    """
    CODE_OPS = "code_ops"
    QA = "qa"
    # Future: PLANNING = "planning", SECURITY_SCAN = "security_scan", etc.


@dataclass(slots=True)
class AskSpec:
    """
    Parameterisation object that travels with the pipeline and determines how
    the LLM should be configured for this run/batch.

    Provider-agnostic: the router translates AskSpec into a concrete LLM route.
    """

    ask_type: AskType = AskType.CODE_OPS
    profile: str = "docstrings.v1"  # for analytics / routing hints

    # Optional overrides (router fills sensible defaults if None)
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None

    # Structured output selection
    response_format_name: Optional[str] = None  # e.g., "docstrings.v1"
    response_format: Optional[Dict[str, Any]] = None  # explicit schema wins

    # Tools (not used yet, kept for extensibility)
    tools: List[Dict[str, Any]] = field(default_factory=list)
    tool_choice: Optional[str] = None

    def validate(self) -> None:
        """Validate internal consistency and obvious bounds."""
        if self.temperature is not None and not (0.0 <= self.temperature <= 2.0):
            raise ValueError("AskSpec.temperature must be between 0.0 and 2.0")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("AskSpec.max_output_tokens must be > 0")
        # If both response_format_name and response_format are provided, the
        # explicit response_format takes precedence — this is by design.

    # Convenience constructors
    @classmethod
    def for_docstrings(cls) -> "AskSpec":
        """Default profile for schema-constrained docstring updates."""
        return cls(
            ask_type=AskType.CODE_OPS,
            profile="docstrings.v1",
            response_format_name="docstrings.v1",
            temperature=0.1,
            max_output_tokens=2048,
        )

    @classmethod
    def for_qa(cls) -> "AskSpec":
        """Default profile for general QA (free-form, no JSON schema)."""
        return cls(
            ask_type=AskType.QA,
            profile="qa.default",
            response_format_name=None,
            temperature=0.2,
            max_output_tokens=1024,
        )


