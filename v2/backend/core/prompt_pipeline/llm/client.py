#v2/backend/core/prompt_pipeline/llm/client.py
"""
Provider-agnostic LLM client (YAML/loader-injected, no env fallback).

Key points:
- No environment variable reads. The API key must be provided via arguments
  (callers typically obtain it from the centralized loader's secrets).
- Supports both explicit request objects (LlmRequest) and a convenient kwargs
  form used by existing Engine code: client.complete(system=..., user=..., model=..., api_key=...).
- For OpenAI:
    - Prefer the Responses API for 4o/omni/mini/o* families (or when forced).
    - Fall back to Chat Completions if Responses rejects parameters.
- Always returns a dict: {"text": str, "usage": Any}, suitable for Engine.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from v2.backend.core.prompt_pipeline.executor.errors import LlmClientError


@dataclass
class LlmRequest:
    system: str
    user: str
    model: str
    api_key: str
    max_output_tokens: int = 1024
    # For Chat Completions: response_format; for Responses API: mapped to text.format
    response_format: Optional[dict] = None
    temperature: float = 0.2
    extra: Dict[str, Any] = field(default_factory=dict)


class LlmClient:
    """
    Provider-agnostic client with JSON-safe outputs.

    - provider='openai': uses Responses API for 4o/omni/mini/o* models, else Chat Completions.
    - provider='mock'  : synthesizes a valid JSON payload (no network).

    Returns:
        dict: {"text": <assistant JSON string>, "usage": <provider-specific usage or None>}
    """

    def __init__(self, provider: str = "openai", model: Optional[str] = None, http_timeout: int = 60):
        self.provider = (provider or "openai").lower()
        self.default_model = model
        self.http_timeout = http_timeout

    # ------------------------------ public API ------------------------------

    def complete(self, req: Optional[LlmRequest] = None, **kwargs: Any) -> Dict[str, Any]:
        """
        Execute a completion. Accepts either:
          - req: LlmRequest
          - kwargs: system=..., user=..., model=..., api_key=..., (optional) temperature/max_output_tokens/response_format/extra
        """
        if req is None:
            # Build from kwargs for Engine compatibility
            system = kwargs.get("system")
            user = kwargs.get("user")
            model = kwargs.get("model") or self.default_model
            api_key = kwargs.get("api_key")
            temperature = float(kwargs.get("temperature", 0.2))
            max_output_tokens = int(kwargs.get("max_output_tokens", 1024))
            response_format = kwargs.get("response_format")
            extra = dict(kwargs.get("extra") or {})

            if not isinstance(system, str) or not system.strip():
                raise LlmClientError("Missing 'system' text for LLM request")
            if not isinstance(user, str) or not user.strip():
                raise LlmClientError("Missing 'user' text for LLM request")
            if not isinstance(model, str) or not model.strip():
                raise LlmClientError("Missing 'model' for LLM request")
            if not isinstance(api_key, str) or not api_key.strip():
                raise LlmClientError("Missing API key for LLM request")

            req = LlmRequest(
                system=system,
                user=user,
                model=model,
                api_key=api_key,
                max_output_tokens=max_output_tokens,
                response_format=response_format,
                temperature=temperature,
                extra=extra,
            )

        if self.provider == "mock":
            text = self._mock_complete_text(req)
            return {"text": text, "usage": None}

        if self.provider == "openai":
            text, usage = self._openai_complete_text(req)
            return {"text": text, "usage": usage}

        raise LlmClientError(f"Unsupported provider: {self.provider}")

    # ------------------------------ OpenAI ------------------------------

    def _openai_complete_text(self, req: LlmRequest) -> tuple[str, Any]:
        """
        OpenAI client:
        - Prefer Responses API for 4o/omni/mini/o* models (or force via req.extra['force_api']='responses').
        - Map Chat-style response_format to Responses text.format:
            * {"type":"json_object"}           -> "json"
            * {"type":"json_schema", ...}      -> {"type":"json_schema","json_schema":{...},"strict":bool}
        - Use Responses API input parts with content type 'input_text'.
        - Never forward req.extra wholesale to OpenAI (only safe keys).
        - Fallback to Chat Completions when Responses rejects parameters.
        Returns:
            (assistant JSON string, usage or None)
        """
        api_key = req.api_key
        session = requests.Session()
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        # Local-only override (not sent to API)
        force_api = (req.extra or {}).get("force_api", "auto").lower()
        model_l = (req.model or "").lower()
        auto_responses = any(tag in model_l for tag in ("gpt-4o", "o4", "omni", "mini"))

        if force_api == "chat":
            return self._openai_chat_fallback_text(req, session, headers)

        use_responses_api = (force_api == "responses") or (force_api == "auto" and auto_responses)
        if use_responses_api:
            # Map response_format to Responses API text.format
            rf = req.response_format or {"type": "json_object"}
            if isinstance(rf, dict) and rf.get("type") == "json_schema":
                js = rf.get("json_schema") or rf.get("schema") or {}
                strict = bool(rf.get("strict", True))
                text_format: str | dict = {"type": "json_schema", "json_schema": js, "strict": strict}
            else:
                text_format = "json"  # JSON mode for Responses API

            payload: Dict[str, Any] = {
                "model": req.model,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"SYSTEM:\n{req.system}\n\nUSER:\n{req.user}",
                            }
                        ],
                    }
                ],
                "max_output_tokens": req.max_output_tokens,
                "temperature": req.temperature,
                "text": {"format": text_format},
            }
            url = "https://api.openai.com/v1/responses"
            try:
                resp = session.post(url, headers=headers, json=payload, timeout=self.http_timeout)
            except requests.RequestException as e:
                raise LlmClientError(f"OpenAI request failed: {e}") from e

            if resp.status_code >= 400:
                msg = self._safe_text(resp)
                # Fall back to Chat if the server rejects any of these params
                if ("Unsupported parameter" in msg) or ("Unknown parameter" in msg) or ("response_format" in msg) or (
                    "text.format" in msg
                ):
                    return self._openai_chat_fallback_text(req, session, headers)
                raise LlmClientError(f"OpenAI error {resp.status_code}: {msg}")

            try:
                data = resp.json()
            except ValueError:
                raise LlmClientError("OpenAI returned non-JSON response (Responses API)")

            text = self._extract_text_from_responses_api(data) or self._fallback_extract_any_json_text(data)
            if not text:
                # Final safety net: try Chat path
                return self._openai_chat_fallback_text(req, session, headers)

            usage = data.get("usage") if isinstance(data, dict) else None
            return text, usage

        # Chat Completions path (JSON mode via response_format)
        return self._openai_chat_fallback_text(req, session, headers)

    def _openai_chat_fallback_text(self, req: LlmRequest, session: requests.Session, headers: Dict[str, str]) -> tuple[str, Any]:
        """Force Chat Completions with JSON mode."""
        url = "https://api.openai.com/v1/chat/completions"
        payload: Dict[str, Any] = {
            "model": req.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
            "temperature": req.temperature,
            "max_tokens": req.max_output_tokens,
            "response_format": {"type": "json_object"},
        }

        # Do NOT forward unknown keys.
        extras = dict(req.extra or {})
        extras.pop("response_format", None)
        if extras:
            payload.update(extras)

        try:
            resp = session.post(url, headers=headers, json=payload, timeout=self.http_timeout)
        except requests.RequestException as e:
            raise LlmClientError(f"OpenAI request failed: {e}") from e

        if resp.status_code >= 400:
            raise LlmClientError(f"OpenAI error {resp.status_code}: {self._safe_text(resp)}")

        try:
            data = resp.json()
        except ValueError:
            raise LlmClientError("OpenAI returned non-JSON response (Chat Completions)")

        text = self._extract_text_from_chat_completions(data) or self._fallback_extract_any_json_text(data)
        if not text:
            raise LlmClientError("OpenAI: could not extract JSON text from Chat Completions payload")

        usage = data.get("usage") if isinstance(data, dict) else None
        return text, usage

    # --- extractors ---

    @staticmethod
    def _extract_text_from_responses_api(data: Dict[str, Any]) -> Optional[str]:
        # Prefer output_text; otherwise find first text part.
        text = data.get("output_text")
        if isinstance(text, str) and text.strip():
            return text
        try:
            out = data.get("output") or []
            if out:
                content = out[0].get("content") or []
                for part in content:
                    if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                        t = part.get("text")
                        if isinstance(t, str) and t.strip():
                            return t
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_text_from_chat_completions(data: Dict[str, Any]) -> Optional[str]:
        try:
            choices = data.get("choices") or []
            if not choices:
                return None
            msg = choices[0].get("message") or {}
            text = msg.get("content")
            if isinstance(text, str) and text.strip():
                return text
        except Exception:
            pass
        return None

    @staticmethod
    def _fallback_extract_any_json_text(data: Dict[str, Any]) -> Optional[str]:
        # Greedy largest balanced {...} scan over the payload string
        try:
            raw = json.dumps(data, ensure_ascii=False)
        except Exception:
            raw = str(data)

        start = raw.find("{")
        if start < 0:
            return None

        depth = 0
        end = -1
        for i, ch in enumerate(raw[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end < 0:
            return None

        candidate = raw[start : end + 1]
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            return None

    @staticmethod
    def _safe_text(resp: requests.Response) -> str:
        try:
            return resp.text[:1200]
        except Exception:
            return ""

    # ------------------------------ Mock ------------------------------

    def _mock_complete_text(self, req: LlmRequest) -> str:
        ids_modes_sigs = self._parse_items_from_user_prompt(req.user)
        items: List[Dict[str, Any]] = []
        for rid, mode, sig in ids_modes_sigs:
            items.append({"id": rid, "mode": (mode or "rewrite"), "docstring": self._synth_doc(sig)})
        if not items:
            items = [{"id": "0", "mode": "rewrite", "docstring": "Summary.\n\n"}]
        return json.dumps({"items": items}, ensure_ascii=False)

    @staticmethod
    def _parse_items_from_user_prompt(user_text: str) -> List[tuple[str, str, str]]:
        blocks = re.split(r"^\s*---\s*$", user_text, flags=re.MULTILINE)
        out: List[tuple[str, str, str]] = []
        for b in blocks:
            b = b.strip()
            if not b:
                continue
            m_id = re.search(r"^id:\s*(.+)$", b, flags=re.MULTILINE)
            m_mode = re.search(r"^mode:\s*(\w+)$", b, flags=re.MULTILINE)
            m_sig = re.search(r"^signature:\s*(.+)$", b, flags=re.MULTILINE)
            if m_id and m_sig:
                rid = m_id.group(1).strip()
                mode = (m_mode.group(1).strip().lower() if m_mode else "rewrite")
                sig = m_sig.group(1).strip()
                out.append((rid, mode, sig))
        return out

    @staticmethod
    def _synth_doc(signature: str) -> str:
        # multi-line pep257-ish placeholder
        import re as _re

        summary = "Describe the function’s purpose succinctly."
        params: List[str] = []
        m = _re.search(r"\((.*)\)", signature)
        if m:
            inside = m.group(1).strip()
            if inside and inside not in ("self", "cls"):
                for part in inside.split(","):
                    name = part.strip().split(":")[0].split("=")[0].strip()
                    if name and name not in ("self", "cls", "/", "*"):
                        params.append(name)
        ret = None
        mret = _re.search(r"->\s*([^\s:]+)", signature)
        if mret:
            typ = mret.group(1)
            if typ and typ.lower() != "none":
                ret = typ

        lines = [summary, ""]
        if params:
            lines.append("Args:")
            for p in params:
                lines.append(f"    {p}: Description.")
        if ret:
            lines.append("")
            lines.append("Returns:")
            lines.append(f"    {ret}: Description.")
        return "\n".join(lines) + "\n"

