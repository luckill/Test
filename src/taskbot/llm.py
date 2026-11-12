"""What every model call in the service shares."""

from __future__ import annotations

from typing import Any

DEFAULT_MODEL = "claude-opus-5-5"
# A response that hits this limit is treated as a failure, never as an answer.
MAX_TOKENS = 16000

# If a safety classifier declines a request, the API re-runs it on a fallback
# model inside the same call.
REFUSAL_FALLBACK: dict[str, Any] = {
    "betas": ["server-side-fallback-2026-07-01"],
    "fallbacks": "default",
}


class ModelError(Exception):
    """The model gave no usable answer. API errors are not wrapped in this."""


def require_complete(response: Any, ticket_key: str) -> None:
    """Raise unless the model finished its answer."""
    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None)
        raise ModelError(f"{ticket_key}: the model declined the request ({category or 'no category'})")
    if response.stop_reason == "max_tokens":
        raise ModelError(f"{ticket_key}: the answer was cut off at {MAX_TOKENS} tokens")
