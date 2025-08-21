from __future__ import annotations
import json, re
from typing import Any, Iterable, List, Dict
from v2.backend.core.prompt_pipeline.executor.errors import ValidationError

class ResponseParser:
    def __init__(self, expected_ids: Iterable[str]):
        self.expected = set(map(str, expected_ids))

    def _coerce(self, raw: str) -> Any:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and "items" in obj: return obj["items"]
            return obj
        except json.JSONDecodeError:
            m = re.search(r'(\[\s*\{.*\}\s*\])', raw, flags=re.DOTALL)
            if not m: raise ValidationError("Response is not valid JSON array")
            return json.loads(m.group(1))

    def parse(self, raw: str) -> List[Dict[str, Any]]:
        data = self._coerce(raw)
        if not isinstance(data, list): raise ValidationError("Top-level JSON must be an array or object with items[]")
        out: List[Dict[str, Any]] = []; seen: set[str] = set()
        for i, item in enumerate(data):
            if not isinstance(item, dict): raise ValidationError(f"Item {i} not an object")
            _id = str(item.get("id", "")).strip(); _doc = item.get("docstring")
            if not _id: raise ValidationError(f"Item {i} missing id")
            if _id in seen: continue
            if not isinstance(_doc, str) or not _doc.strip(): raise ValidationError(f"Item {_id} has empty docstring")
            out.append({"id": _id, "docstring": _doc, "notes": item.get("notes")}); seen.add(_id)
        missing = self.expected.difference({o["id"] for o in out})
        if missing: raise ValidationError(f"Missing ids in response: {sorted(missing)[:5]}")
        return out
