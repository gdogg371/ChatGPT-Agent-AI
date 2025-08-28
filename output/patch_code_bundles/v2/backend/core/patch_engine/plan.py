from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Literal, Union


Anchor = Literal[
    "file_start",
    "file_end",
    "after_shebang_and_encoding",
    "after_line",
    "before_line",
    "after_import_block",
]


@dataclass
class ReplaceRange:
    """
    Replace an inclusive 1-based line range [start_line, end_line] with new_text.
    """
    relpath: str
    start_line: int  # inclusive (1-based)
    end_line: int    # inclusive (1-based)
    new_text: str


@dataclass
class InsertAt:
    """
    Insert new_text at an anchor.

    For anchors 'after_line' and 'before_line', you must set 'line' (1-based).
    'line' is ignored for other anchors.
    """
    relpath: str
    anchor: Anchor
    line: Optional[int]  # used only for after_line/before_line
    new_text: str


@dataclass
class DeleteRange:
    """
    Delete an inclusive 1-based line range [start_line, end_line].
    """
    relpath: str
    start_line: int
    end_line: int


@dataclass
class AddFile:
    """
    Create a new file with content (overwrites if already exists).
    """
    relpath: str
    content: str


@dataclass
class DeleteFile:
    """
    Remove a file at relpath (no-op if missing).
    """
    relpath: str


PatchOp = Union[ReplaceRange, InsertAt, DeleteRange, AddFile, DeleteFile]


@dataclass
class PatchPlan:
    """
    A list of generic, domain-agnostic patch operations.
    """
    ops: List[PatchOp]
