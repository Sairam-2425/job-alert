"""LLM wrapper for any OpenAI-compatible API (NVIDIA NIM, OpenAI, ...): JSON output, timeout, retry."""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import openai

from .utils import retry_call

log = logging.getLogger(__name__)

TRANSIENT_ERRORS = (
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
)
FATAL_ERRORS = (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError)

_json_mode_supported = True  # flips to False if the provider rejects response_format


class LLMError(Exception):
    """The model returned something unusable (not JSON, wrong shape)."""


def api_key() -> Optional[str]:
    return os.environ.get("NVIDIA_API_KEY") or os.environ.get("OPENAI_API_KEY")


def get_client(timeout: float, base_url: Optional[str] = None) -> openai.OpenAI:
    base_url = os.environ.get("OPENAI_BASE_URL") or base_url or None
    # max_retries=0: we do our own retries so they are logged and bounded.
    return openai.OpenAI(api_key=api_key(), base_url=base_url, timeout=timeout, max_retries=0)


def extract_json(text: str) -> dict:
    """Parse a JSON object even if the model wrapped it in ``` fences or added chatter."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("Model did not return a JSON object")
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model did not return valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("Model JSON was not an object")
    return data


def chat_json(client, *, model: str, system: str, user: str, attempts: int = 4) -> dict:
    global _json_mode_supported

    def _call() -> str:
        global _json_mode_supported
        kwargs = dict(
            model=model,
            temperature=0,
            max_tokens=2048,
            messages=[
                {"role": "system", "content": system + "\n\nReturn ONLY the JSON object, no other text."},
                {"role": "user", "content": user},
            ],
        )
        if _json_mode_supported:
            try:
                resp = client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
                return resp.choices[0].message.content or ""
            except (openai.BadRequestError, openai.UnprocessableEntityError):
                log.warning("Provider rejected JSON mode - falling back to prompt-only JSON")
                _json_mode_supported = False
        resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    content = retry_call(
        _call,
        attempts=attempts,
        retry_on=TRANSIENT_ERRORS,
        give_up_on=FATAL_ERRORS,
        what="llm.chat",
    )
    return extract_json(content)
