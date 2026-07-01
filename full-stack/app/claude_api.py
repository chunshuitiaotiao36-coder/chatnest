"""Direct Anthropic API backend — fallback when Max/CC subscription is unavailable."""

import logging
import os
from collections.abc import AsyncGenerator, Callable
from uuid import uuid4

import anthropic

from app.claude import (
    available_models,
    build_system_prompt,
    thinking_options,
)
from app.store import conversation_messages

logger = logging.getLogger(__name__)


def _get_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _build_history(conv_id: str) -> list[dict]:
    """Reconstruct Anthropic API messages array from stored conversation."""
    rows, _, _ = conversation_messages(conv_id)
    messages: list[dict] = []
    for msg in rows:
        role = msg["role"]
        text = msg.get("text") or ""
        thinking = msg.get("thinking") or ""
        if role == "assistant" and thinking:
            content = [{"type": "thinking", "thinking": thinking, "signature": ""}]
            if text:
                content.append({"type": "text", "text": text})
            messages.append({"role": "assistant", "content": content})
        elif text:
            messages.append({"role": role, "content": text})
    return messages


async def stream_chat_api(
    message: str,
    conv_id: str,
    model: str = "claude-sonnet-4-6",
    effort: str = "medium",
    extended: bool = True,
    timing_callback: Callable[[str], None] | None = None,
) -> AsyncGenerator[dict, None]:
    model_config = next(
        (m for m in available_models() if m["id"] == model), None
    )
    if model_config is None:
        raise ValueError("unsupported model")

    thinking_cfg, _ = thinking_options(model_config, effort, extended)
    system_prompt = await build_system_prompt(message, model)

    history = _build_history(conv_id)
    if not history or history[-1].get("content") != message:
        history.append({"role": "user", "content": message})

    kwargs: dict = {
        "model": model,
        "system": system_prompt,
        "messages": history,
        "max_tokens": 16384,
    }
    if thinking_cfg.get("type") == "enabled":
        kwargs["thinking"] = thinking_cfg
        budget = thinking_cfg.get("budget_tokens", 10000)
        kwargs["max_tokens"] = max(16384, budget + 8192)
    elif thinking_cfg.get("type") == "adaptive":
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": 10000}
        kwargs["max_tokens"] = max(16384, 10000 + 8192)

    client = _get_client()
    first_text = False
    session_id = f"api-{uuid4().hex[:12]}"

    try:
        async with client.messages.stream(**kwargs) as stream:
            if timing_callback:
                timing_callback("sdk_first_event")
            async for event in stream:
                if event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "thinking_delta":
                        yield {"event": "thinking", "text": delta.thinking}
                    elif delta.type == "text_delta":
                        if not first_text:
                            first_text = True
                            if timing_callback:
                                timing_callback("first_text_token")
                        yield {"event": "delta", "text": delta.text}
        yield {"event": "done", "session_id": session_id}
    finally:
        await client.close()
