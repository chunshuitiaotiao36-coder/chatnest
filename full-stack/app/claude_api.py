"""第二条线：直连 Anthropic SDK，当订阅/激活线路打不通时顶上。

🔴 08-30：这个文件写好之后**一直没有任何地方 import 它**（全仓库 grep 过），
   等于双线只做了一半。代价是 pro 过期那一个多星期，keepalive / nightguard /
   寄相思回信 三条后台线一起抛异常、各自被 except Exception 吞掉，她一条消息、
   一封信都没收到，而日志没有人看。现在由 claude.background_stream() 接上。

🔴 不碰 os.environ。切换激活线路是进程级操作（relays._apply_env），后台线在她
   聊天的时候动环境变量会串线。所以这里把 base_url / api_key 当参数显式传进来，
   谁也不影响谁。
"""

import logging
import os
from collections.abc import AsyncGenerator, Callable
from uuid import uuid4

import anthropic

from app.claude import (
    available_models,
    build_system_prompt,
    build_user_prompt,
    thinking_options,
)
from app.store import conversation_messages

logger = logging.getLogger(__name__)


def _get_client(base_url: str = "", api_key: str = "") -> anthropic.AsyncAnthropic:
    """显式凭据优先；都不给才回落到环境变量（保留原来的行为）。"""
    kwargs: dict = {"api_key": api_key or os.environ.get("ANTHROPIC_API_KEY", "")}
    if base_url:
        kwargs["base_url"] = base_url
    return anthropic.AsyncAnthropic(**kwargs)


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
    base_url: str = "",
    api_key: str = "",
    models: list[dict] | None = None,
) -> AsyncGenerator[dict, None]:
    # 🔴 模型要从**这条线路**的列表里查，不能用 available_models()——那个跟着
    #    当前激活线路走，而我们正是因为那条线路打不通才走到这儿的。
    #    背景见 claude.background_model 的注释：裸 id 查不到会在第一行就 raise。
    pool = models if models is not None else available_models()
    model_config = next((m for m in pool if m["id"] == model), None)
    if model_config is None:
        raise ValueError(f"unsupported model: {model}")

    thinking_cfg, _ = thinking_options(model_config, effort, extended)
    system_prompt = build_system_prompt(model)
    prompt = await build_user_prompt(message, conv_id)

    history = _build_history(conv_id)
    if history and history[-1].get("role") == "user" and history[-1].get("content") == message:
        # already persisted by the caller — swap in the recall-wrapped copy
        history[-1]["content"] = prompt
    else:
        history.append({"role": "user", "content": prompt})

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

    client = _get_client(base_url, api_key)
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
