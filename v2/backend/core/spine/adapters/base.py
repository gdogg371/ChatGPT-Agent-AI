# SPDX-License-Identifier: MIT
# File: v2/backend/core/spine/adapters/base.py
from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from ..contracts import Task, Artifact


@runtime_checkable
class Adapter(Protocol):
    """
    Spine adapter Protocol — a thin edge shim around an existing module.
    Implementations may also implement the existing TaskAdapter interface;
    this Protocol is intentionally compatible with that shape.

    Required:
      - capability: str
      - to_native(Task)   -> Any   (validate + translate Task to module-specific call)
      - from_native(Any)  -> Artifact | list[Artifact]
    Optional (kept for compatibility with existing pipeline):
      - sanitize(payload, item) -> str
      - apply(original_src, item, payload) -> str
      - verify(payload, item) -> (ok: bool, issues: list[str])
      - build_items(suspects) -> list[dict]
      - parse_response(raw, expected_ids) -> list[dict]
    """
    capability: str

    def to_native(self, task: Task) -> Any:
        ...

    def from_native(self, result: Any) -> Artifact | list[Artifact]:
        ...
