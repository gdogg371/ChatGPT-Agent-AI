from __future__ import annotations
from typing import TypedDict, NotRequired, List, Dict

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
    symbol: str             # simple/dotted name
    docstring: str          # full current docstring (may be empty)
    signature: str          # def/class signature
    pre_context: str        # small window before
    post_context: str       # small window after

class BatchItem(TypedDict):
    id: str
    path: str
    relpath: str
    signature: str
    current_docstring: str
    local_context: str
    target_lineno: int

class ParsedResult(TypedDict):
    id: str
    docstring: str
    notes: NotRequired[str]

class PromptPayload(TypedDict):
    system: str
    user: str

class PromptBundle(TypedDict):
    ids: List[str]
    messages: PromptPayload
