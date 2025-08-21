from __future__ import annotations

from v2.backend.core.types.types import AskSpec
from v2.backend.core.tasks.base import TaskAdapter
from v2.backend.core.tasks.adapter import DocstringsAdapter  # <-- correct path


def select_task_adapter(ask_spec: AskSpec) -> TaskAdapter:
    """
    Resolve the appropriate TaskAdapter for the current run.

    For now, we support the Docstrings task. Additional adapters can be added
    and selected based on ask_spec.ask_type / ask_spec.profile in the future.
    """
    profile = (ask_spec.profile or "").lower()
    ask_type = (
        ask_spec.ask_type.value if hasattr(ask_spec, "ask_type") else str(ask_spec.ask_type)
    ).lower()

    # Heuristics: docstrings when explicitly requested via profile or ask type
    if "docstring" in profile or ask_type in ("code_ops", "docstrings"):
        return DocstringsAdapter()

    # Default to docstrings until additional adapters are implemented
    return DocstringsAdapter()

