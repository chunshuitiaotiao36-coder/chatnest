"""Single-owner actor for one persistent Claude SDK client."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from claude_agent_sdk.types import (
    AssistantMessage as _AssistantMessage,
    UserMessage as _UserMessage,
    TextBlock as _TextBlock,
    ToolUseBlock as _ToolUseBlock,
    ToolResultBlock as _ToolResultBlock,
)
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
)
from claude_agent_sdk.types import StreamEvent


logger = logging.getLogger(__name__)
# this project configures no logging at all, so a root-attached logger sits at
# WARNING and .info() goes nowhere. uvicorn configures its own — borrow it.
cache_logger = logging.getLogger("uvicorn.error")


# CLI 不发结束标记时 receive_response() 会永远挂着，用户只看到一个空白气泡。
# 180 秒：实测正常首字 3.3-6.6 秒，长回复带思考和工具调用可能到几十秒，
# 这个值足够宽，不会误伤真实的长回复。
RESPONSE_TIMEOUT_SECONDS = 180


class ActorBusyError(RuntimeError):
    pass


class ActorTimeoutError(RuntimeError):
    pass


@dataclass
class TurnRequest:
    prompt: str
    options: ClaudeAgentOptions
    fingerprint: str
    timing_callback: Callable[[str], None] | None
    outbox: asyncio.Queue
    force_fresh: bool = False


class ConvActor:
    def __init__(self, conv_id: str, project_dir: str) -> None:
        self.conv_id = conv_id
        self.project_dir = project_dir
        self.last_active = monotonic()
        self.busy = False
        self.closed = False
        self._client: ClaudeSDKClient | None = None
        self._fingerprint: str | None = None
        self._inbox: asyncio.Queue[TurnRequest | None] = asyncio.Queue()
        self._state_lock = asyncio.Lock()
        self._task = asyncio.create_task(
            self._run(),
            name=f"claude-actor-{conv_id[:8]}",
        )

    @property
    def alive(self) -> bool:
        return not self.closed and not self._task.done()

    def is_warm_for(self, fingerprint: str) -> bool:
        return (
            self.alive
            and self._client is not None
            and self._fingerprint == fingerprint
        )

    async def submit(
        self,
        prompt: str,
        options: ClaudeAgentOptions,
        fingerprint: str,
        timing_callback: Callable[[str], None] | None,
        force_fresh: bool = False,
    ) -> asyncio.Queue:
        async with self._state_lock:
            if not self.alive:
                raise RuntimeError("Claude 会话连接已失效")
            if self.busy:
                raise ActorBusyError("上一条消息仍在回复")
            self.busy = True
        outbox: asyncio.Queue = asyncio.Queue()
        await self._inbox.put(
            TurnRequest(
                prompt=prompt,
                options=options,
                fingerprint=fingerprint,
                timing_callback=timing_callback,
                outbox=outbox,
                force_fresh=force_fresh,
            )
        )
        return outbox

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._inbox.put(None)
        await self._task

    async def _disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                logger.exception("Persistent Claude client disconnect failed")
            finally:
                self._client = None
                self._fingerprint = None

    async def _ensure_client(self, request: TurnRequest) -> None:
        if (
            not request.force_fresh
            and self._client is not None
            and self._fingerprint == request.fingerprint
        ):
            return
        await self._disconnect()
        client = ClaudeSDKClient(request.options)
        await client.connect()
        self._client = client
        self._fingerprint = request.fingerprint

    async def _handle_turn(self, request: TurnRequest) -> None:
        callback = request.timing_callback
        await self._ensure_client(request)
        if self._client is None:
            raise RuntimeError("Claude 会话连接失败")

        await self._client.query(request.prompt)
        first_sdk_event_seen = False
        first_text_token_seen = False
        got_streaming_text = False
        result_seen = False
        # 上游实际用的模型 ID。配置里写 `opus` 时 CLI 会自己解析成某个具体版本，
        # 只有 AssistantMessage.model 说得准。去重是因为一轮里可能有多条
        # AssistantMessage（多轮工具调用），不去重会刷屏。
        reported_model = ""
        # CLI 不发结束标记时这个循环会永远挂着（订阅线路上实测过：没有 first_text_token、
        # 没有 ResultMessage、也没有异常）。超时后连同子进程一起收掉，
        # 别把一个已知坏掉的进程留给下一轮。
        try:
            async with asyncio.timeout(RESPONSE_TIMEOUT_SECONDS):
                async for sdk_message in self._client.receive_response():
                    if not first_sdk_event_seen:
                        first_sdk_event_seen = True
                        if callback:
                            callback("sdk_first_event")
                    if isinstance(sdk_message, SystemMessage):
                        if sdk_message.subtype == "init":
                            initialized_cwd = sdk_message.data.get("cwd")
                            if initialized_cwd != self.project_dir:
                                raise RuntimeError("会话恢复失败")
                    elif isinstance(sdk_message, StreamEvent):
                        event = sdk_message.event
                        if event.get("type") != "content_block_delta":
                            continue
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text and not first_text_token_seen:
                                first_text_token_seen = True
                                if callback:
                                    callback("first_text_token")
                            got_streaming_text = True
                            await request.outbox.put({"event": "delta", "text": text})
                        elif delta.get("type") == "thinking_delta":
                            await request.outbox.put(
                                {"event": "thinking", "text": delta.get("thinking", "")}
                            )
                    elif isinstance(sdk_message, (_AssistantMessage, _UserMessage)):
                        # _UserMessage 没有 model 字段，所以必须 getattr 带默认值。
                        # 不挂 ResultMessage：那要等整轮结束，而这里在正文开始流的
                        # 时候就到了，顶栏能立刻更新。
                        model_id = getattr(sdk_message, "model", "") or ""
                        if model_id and model_id != reported_model:
                            reported_model = model_id
                            await request.outbox.put({"event": "model", "id": model_id})
                        for block in getattr(sdk_message, "content", []) or []:
                            if isinstance(block, _TextBlock) and not got_streaming_text:
                                text = block.text or ""
                                if text and not first_text_token_seen:
                                    first_text_token_seen = True
                                    if callback:
                                        callback("first_text_token")
                                if text:
                                    await request.outbox.put({"event": "delta", "text": text})
                            elif isinstance(block, _ToolUseBlock):
                                await request.outbox.put({
                                    "event": "tool_use",
                                    "id": block.id,
                                    "name": block.name,
                                    "input": block.input,
                                })
                            elif isinstance(block, _ToolResultBlock):
                                content = block.content
                                if isinstance(content, list):
                                    content = "".join(
                                        c.get("text", "") if isinstance(c, dict) else str(c)
                                        for c in content
                                    )
                                await request.outbox.put({
                                    "event": "tool_result",
                                    "tool_use_id": block.tool_use_id,
                                    "content": content or "",
                                    "is_error": bool(block.is_error),
                                })
                    elif isinstance(sdk_message, ResultMessage):
                        result_seen = True
                        usage = sdk_message.usage or {}
                        cache_logger.info(
                            "cache_usage model=%s stop=%s turns=%s text=%s input=%s cache_create=%s cache_read=%s cost=%s",
                            ",".join((sdk_message.model_usage or {}).keys()) or "?",
                            getattr(sdk_message, "stop_reason", None),
                            getattr(sdk_message, "num_turns", None),
                            first_text_token_seen,
                            usage.get("input_tokens"),
                            usage.get("cache_creation_input_tokens"),
                            usage.get("cache_read_input_tokens"),
                            sdk_message.total_cost_usd,
                        )
                        await request.outbox.put(
                            {"event": "done", "session_id": sdk_message.session_id}
                        )
                    else:
                        cache_logger.info("sdk_unhandled: %s", type(sdk_message).__name__)
        except TimeoutError:
            cache_logger.warning(
                "sdk_response_timeout after %ss, killing actor",
                RESPONSE_TIMEOUT_SECONDS,
            )
            await self._disconnect()
            raise ActorTimeoutError(
                f"Claude 超过 {RESPONSE_TIMEOUT_SECONDS} 秒没有返回完整回复，已断开重来"
            ) from None
        if not result_seen:
            raise RuntimeError("Claude 连接提前结束")

    async def _run(self) -> None:
        try:
            while True:
                request = await self._inbox.get()
                if request is None:
                    break
                try:
                    await self._handle_turn(request)
                except Exception as exc:
                    await request.outbox.put(exc)
                    await self._disconnect()
                finally:
                    self.last_active = monotonic()
                    async with self._state_lock:
                        self.busy = False
                    await request.outbox.put(None)
        finally:
            await self._disconnect()
            self.closed = True
