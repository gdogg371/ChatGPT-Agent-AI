from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, Iterable, Dict, Any, List

@dataclass
class Item:
    id: str
    relpath: str
    lineno: int
    source: str | None = None
    meta: Dict[str, Any] | None = None

@dataclass
class Prompt:
    system: str
    user: str

@dataclass
class Result:
    id: str
    diagnostics: Dict[str, Any]
    new_docstring: str | None = None

class TaskAdapter(Protocol):
    task_name: str
    def prepare_items(self, rows: Iterable[Item]) -> List[Item]: ...
    def build_prompt(self, batch: List[Item]) -> Prompt: ...
    def parse_response(self, raw: Dict[str, Any]) -> Dict[str, Result]: ...
    def verify(self, item: Item, result: Result) -> List[str]: ...
    def apply(self, item: Item, result: Result) -> str: ...
