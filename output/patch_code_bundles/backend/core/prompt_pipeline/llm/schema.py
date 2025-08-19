from __future__ import annotations
from typing import List, Dict, Any, Optional

def _item_schema_for_id(id_str: str) -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "mode", "docstring"],
        "properties": {
            "id": {"type": "string", "const": id_str},
            "mode": {"type": "string", "enum": ["rewrite", "create"]},
            "docstring": {"type": "string", "minLength": 1},
        },
    }

def openai_response_format(expected_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    If expected_ids provided, build a JSON Schema that forces those exact ids
    (one object per id, in any order). Strict mode ON for Responses API.
    For Chat Completions, caller may ignore the schema and just use json_object mode.
    """
    if expected_ids:
        schema_items = [_item_schema_for_id(i) for i in expected_ids]
        schema: Dict[str, Any] = {
            "name": "DocstringBatch",
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["items"],
                "properties": {
                    "items": {
                        "type": "array",
                        # allow any order but require exact count of items
                        "minItems": len(expected_ids),
                        "maxItems": len(expected_ids),
                        "items": schema_items if len(expected_ids) == 1 else {"anyOf": schema_items},
                    }
                },
            },
            "strict": True,
        }
        return {"type": "json_schema", "json_schema": schema, "strict": True}
    # default JSON mode
    return {"type": "json_object"}
