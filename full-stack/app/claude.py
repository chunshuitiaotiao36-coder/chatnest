"""Claude Agent SDK wrapper with streaming, thinking, and session resume."""

import asyncio
import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    get_session_info,
)
from claude_agent_sdk.types import StreamEvent

from app import relays, store
from app.actor import ActorBusyError
from app.memory import build_profile_context, memory_tool_permission, read_memory
from app.registry import get_registry


# 2G 上第二个并发摘要子进程没有任何收益：摘要是装饰性的，晚两秒出来无所谓，
# 而 100-200MB 是实打实的。峰值从「主回复 + 2 摘要」降到「主回复 + 1 摘要」。
_haiku_sem = asyncio.Semaphore(1)


def _summary_model() -> str:
    """摘要要用当前线路上真实存在的模型。硬编码 ID 换条线路就必然失败，
    而失败之前 Node 子进程已经起来了——2G 上这是白付一份内存。
    按 haiku > sonnet > 最后一条 挑：订阅线路有 haiku 别名，
    中转站种子里 Sonnet 4.6 本来就是标了 primary:false 的摘要位。"""
    try:
        ids = [m["id"] for m in relays.active_models_rich() if m.get("id")]
    except Exception:
        cli_logger.exception("摘要模型挑选失败")
        return ""
    if not ids:
        return ""
    # 订阅线路的真实模型列表里没有 haiku，于是第一个命中 "sonnet" 的是
    # Sonnet 5 —— 拿最贵的档去写 20 字摘要。把便宜的版本排在裸 "sonnet" 前面。
    # 不要改成"优先挑 primary:false"：那个字段已经被抹平成全 true（队列 27）。
    for want in ("haiku", "sonnet-4-6", "sonnet-4", "sonnet"):
        for mid in ids:
            if want in mid.lower():
                return mid
    return ids[-1]

MEMORY_SEARCH_URL = os.environ.get("MEMORY_SEARCH_URL", "http://127.0.0.1:3900/search")
MEMORY_SEARCH_TOP_K = 6
MEMORY_SEARCH_BUDGET_CHARS = 1500
MEMORY_SEARCH_TIMEOUT_S = 2.0
memlog = logging.getLogger("memory.inject")
# 借 uvicorn.error：全仓库没有 logging 配置，root logger 默认 WARNING 会把 .info() 吞掉
cli_logger = logging.getLogger("uvicorn.error")

OMBRE_MCP_URL = os.environ.get("OMBRE_MCP_URL", "")
OMBRE_MCP_TOKEN = os.environ.get("OMBRE_MCP_TOKEN", "")


def ombre_mcp_servers() -> dict:
    if not OMBRE_MCP_URL:
        return {}
    return {
        "ombre": {
            "type": "http",
            "url": OMBRE_MCP_URL,
            "headers": {"X-Admin-Token": OMBRE_MCP_TOKEN} if OMBRE_MCP_TOKEN else {},
        }
    }


async def fetch_memory_hits(query: str) -> str:
    """POST to local hybrid-search service. Silent-fail on any error."""
    if not query.strip():
        return ""

    def _call() -> dict | None:
        body = json.dumps(
            {
                "query": query,
                "top_k": MEMORY_SEARCH_TOP_K,
                "budget_chars": MEMORY_SEARCH_BUDGET_CHARS,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            MEMORY_SEARCH_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=MEMORY_SEARCH_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            memlog.info("memory search unavailable: %s", e)
            return None

    data = await asyncio.to_thread(_call)
    if not data:
        return ""
    joined = (data.get("joined") or "").strip()
    stats = data.get("stats") or {}
    memlog.info(
        "memory search ok: %d chunks, vec=%d bm25=%d, %sms, %d chars injected",
        len(data.get("results") or []),
        stats.get("vec_hits", 0),
        stats.get("bm25_hits", 0),
        stats.get("ms", "?"),
        len(joined),
    )
    return joined


SYSTEM_PROMPT = """\
You are a warm, concise assistant in a personal chat app. Reply naturally, respect the user's saved profile and preferences, and use tools only when they help. When you save long-term memories, save objective user facts rather than conversation summaries.
"""
PROJECT_ROOT = Path(os.environ.get("AGENT_APP_ROOT", Path(__file__).resolve().parent.parent)).expanduser().resolve()
MODELS_PATH = Path(os.environ.get("MODELS_FILE", PROJECT_ROOT / "models.json")).expanduser().resolve()
PROJECT_DIR = str(PROJECT_ROOT)
# TG 那条线专用的轻量人设。副本随镜像走，源文件在
# Loved-Before-Words/小窝prompt-Telegram日常.md。
# 按代码路径找，别跟着 AGENT_APP_ROOT 走：生产上它是 /data（数据卷），
# 而这个文件是随镜像装进 /app 的，跟过去就每轮都读不到。
APP_DIR = Path(__file__).resolve().parent.parent
TELEGRAM_PROMPT_FILE = Path(
    os.environ.get("TELEGRAM_PROMPT_FILE", APP_DIR / "telegram_prompt.md")
).expanduser()
SUMMARY_PROMPT = (
    "你是一个摘要工具。你将收到一段AI的内心思考过程，你的唯一任务是输出一句不超过20字的中文概括。"
    "要求：动词短语开头，写出决策或权衡，不要复述内容，不要加’思考’/’分析’等元描述词。"
    "禁止：不要回复对话，不要加emoji，不要说’我理解’/’让我’/’好的’，"
    "不要出现’接住’及其任何变体（’接住她’/’接住情绪’等一律禁止），"
    "不要输出任何非摘要内容。"
    "只描述思考动作本身，不要推断或描述人物关系，不许出现’女儿’/’妻子’/’朋友’这类身份称谓。"
    "风格参考（动词要多样化，禁止反复使用同一句式）：’决定静默陪伴而非催促’、’梳理求职时间线’、"
    "’用日常语气回应而非讲道理’、’定位z-index层级冲突’、’回忆上次聊过的话题’、"
    "’组织多条建议的优先级’、’拆解前端布局问题’、’斟酌措辞避免说教感’、"
    "’对比两种技术方案利弊’、’补充遗漏的边界情况’、’绕开敏感话题切入正题’、"
    "’核实时间日期再作答’、’顺着她的情绪往下聊’、’挑选最贴切的类比解释’。"
    "只输出摘要本身，不要有任何其他文字。"
)


class SessionResumeError(RuntimeError):
    pass


def available_models() -> list[dict]:
    from app import relays  # local import to avoid startup cycle
    return relays.active_models_rich()


def thinking_options(
    model: dict,
    effort: str,
    extended: bool,
) -> tuple[dict, str | None]:
    if model["thinking"] == "none":
        return {"type": "disabled"}, None
    allowed_efforts = {"low", "medium", "high", "max"}
    selected = effort if effort in allowed_efforts else "medium"
    if model["thinking"] == "adaptive":
        return {"type": "adaptive"}, selected
    if extended:
        return {"type": "enabled", "budget_tokens": 8_000}, selected
    return {"type": "disabled"}, selected


def build_system_prompt(model: str, lean: bool = False) -> str:
    """Stable prefix. Only changes when the user edits their profile/memory —
    never per-turn, so the CLI's prompt cache keeps matching it.

    lean=True 只走 Telegram 那条线：读 telegram_prompt.md，不带 profile、
    不带完整记忆。lean=False 是网页端的路径，行为跟这一单之前一模一样——
    下面那一段一个字符都没动，本地有一条断言拿改动前的实现逐字节比对。
    """
    if lean:
        # 那句 model 声明仍要拼在最前面：她刚为「你是 opus 几答不上来」
        # 改过模型列表，别在这里把它弄丢。
        model_line = (
            f"You are running as model {model}. "
            "If asked which model you are, answer with that identifier."
        )
        try:
            lean_body = TELEGRAM_PROMPT_FILE.read_text(encoding="utf-8").strip()
            # 空文件跟读不到同样处理：宁可啰嗦，不要哑着上一个空人设
            if not lean_body:
                cli_logger.warning(
                    "telegram prompt 是空的（%s），回落到完整版人设", TELEGRAM_PROMPT_FILE
                )
        except OSError as exc:
            lean_body = ""
            cli_logger.warning(
                "telegram prompt 读不到（%s: %s），回落到完整版人设", TELEGRAM_PROMPT_FILE, exc
            )
        if lean_body:
            return f"{model_line}\n\n{lean_body}"
        # 读不到就落到下面的默认路径（完整版）

    profile_context = build_profile_context().strip()
    memory = "" if profile_context else read_memory().strip()
    system_prompt = f"You are running as model {model}. If asked which model you are, answer with that identifier.\n\n{SYSTEM_PROMPT}"
    if profile_context:
        system_prompt += (
            "\n\n以下是用户在 Profile 中保存的资料、长期记忆和模型偏好。"
            "Saved memories 是事实记忆；Preferences 是用户明确要求的回复偏好，"
            "应在不违反系统要求时遵守：\n"
            f"{profile_context}"
        )
    if memory:
        system_prompt += f"\n\n以下是用户明确保存的长期记忆：\n{memory}"
    return system_prompt


async def build_user_prompt(message: str) -> str:
    """Memory recall is volatile (re-retrieved per message), so it must never
    enter system_prompt — that would move the cache-breaking bytes to the very
    front of the request. Riding on the user turn puts it after every cache
    breakpoint, and once written into history it never changes again.

    The recall goes before the user's own words so the last thing the model
    reads is what she actually said."""
    memory_hits = await fetch_memory_hits(message)
    if not memory_hits:
        return message
    return (
        "<memory_recall>\n"
        "以下是从记忆书架向量检索到的相关条目（可能相关也可能没用，"
        "自己判断是否引用；不要照搬，更不要逐字复读）：\n"
        f"{memory_hits}\n"
        "</memory_recall>\n\n"
        f"{message}"
    )


def _record_usage(
    usage: dict | None,
    conv_id: str,
    source: str,
    active_summary: dict,
) -> None:
    """把一轮的 token / 成本写进旁路账本。

    落库放在这里而不是 actor：actor 拿得到 usage 但不知道走的是哪条中转站，
    stream_chat 两样都有（它本来就在调 relays.get_active_summary() 判断订阅）。
    """
    if not usage:
        return
    store.record_usage({
        "source": source,
        "conv_id": conv_id,
        "relay_id": active_summary.get("id"),
        "relay_name": active_summary.get("name"),
        "relay_mode": active_summary.get("mode"),
        "model": usage.get("model"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_create": usage.get("cache_create"),
        "cache_read": usage.get("cache_read"),
        "cost_usd": usage.get("cost_usd"),
    })


async def stream_chat(
    message: str,
    conv_id: str,
    session_id: str | None = None,
    model: str = "claude-sonnet-4-6",
    effort: str = "medium",
    extended: bool = True,
    timing_callback: Callable[[str], None] | None = None,
    lean: bool = False,
    source: str = "web",
) -> AsyncGenerator[dict, None]:
    """lean=True 是 Telegram 那条轻量线：轻量人设 + 精简工具集，
    但**挂 Ombre MCP**（07-31 从「不挂」回退过来，见下面 mcp_servers 那段）。
    默认 False，网页端的行为一个字没变。

    source 只用来给用量账本分「网页 / TG」，不要拿 lean 当它的代理——
    那是两件事，以后会分开。"""
    model_config = next(
        (item for item in available_models() if item["id"] == model),
        None,
    )
    if model_config is None:
        raise ValueError("unsupported model")
    if session_id and get_session_info(session_id, directory=PROJECT_DIR) is None:
        raise SessionResumeError("会话恢复失败")
    thinking, selected_effort = thinking_options(model_config, effort, extended)

    system_prompt = build_system_prompt(model, lean=lean)
    prompt = await build_user_prompt(message)

    # lean 以前同时管两件事：精简 prompt + 不挂 MCP。07-31 把这两件拆开——
    # 精简 prompt 保留，MCP 恢复。
    #
    # 起因是小朵说「之前说过的事好多他都不记得」。她说得对：TG 那份精简人设
    # 里只有「你是谁」，一件「我们发生过什么」都没有——他不是忘了，是从来
    # 没被告诉过。摆了三条路，她选了接 Ombre 而不是往 prompt 里塞锚点事件：
    # 记忆该是要用的时候去查，不是全背在身上，而且**工具调用不碰
    # system_prompt 前缀**，她刚建好的那份缓存一点不受影响；往 prompt 里塞
    # 则是每轮都要付那些 token。
    mcp_servers = ombre_mcp_servers()
    if lean:
        # TG 是聊天，不是干活的地方。Read 必须留着——识图全靠它去读存下来的
        # 图片文件。Bash / Write / Edit / WebSearch 那些在 TG 上都不需要。
        allowed_tools = ["Read"]
    else:
        allowed_tools = ["Read", "Grep", "Glob", "Write", "Edit", "Bash", "WebSearch", "WebFetch", "TodoWrite"]
    if mcp_servers:
        allowed_tools.append("mcp__ombre")
    option_values = dict(
        model=model,
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        mcp_servers=mcp_servers,
        can_use_tool=memory_tool_permission,
        max_turns=8,
        include_partial_messages=True,
        thinking=thinking,
        resume=session_id,
        setting_sources=[],
        cwd=PROJECT_DIR,
        stderr=lambda line: cli_logger.info("cli_stderr: %s", line),
        # Anthropic 的 prompt cache 默认只活 5 分钟，而她的节奏是断续的——
        # 聊两句去吃饭、回来接着说，每次回来都是凉的。07-30 用量面板实测：
        # 间隔近的那轮 cache_read=44858 花 ¥0.19，隔远的两轮 cache_read=0
        # 各花 ¥1.8，差九倍。这个 beta 把 TTL 延到 1 小时。
        # 只加在 stream_chat：三个 summarize_* 是一次性短请求，缓存对它们没意义。
        betas=["extended-cache-ttl-2025-04-11"],
    )
    if selected_effort is not None:
        option_values["effort"] = selected_effort
    options = ClaudeAgentOptions(**option_values)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "model": model,
                "thinking": thinking,
                "effort": selected_effort,
                "system_prompt": system_prompt,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    # 订阅线路上复用的 CLI 子进程从第二次 query 起会静默停止产出内容
    # （实测 cold 2/2 成功、warm 3/4 失败，stderr 空、无异常、ResultMessage 不到）。
    # 根因在 CLI 内部，这里先绕开：订阅模式每次新建子进程。
    # 中转站不受影响，热复用照旧。
    try:
        active_summary = relays.get_active_summary()
        is_subscription = active_summary.get("mode") == "subscription"
    except Exception:
        logging.getLogger("uvicorn.error").exception("relay mode 判定失败，按中转站处理")
        active_summary = {}
        is_subscription = False

    outbox = await get_registry().submit(
        conv_id,
        prompt,
        options,
        fingerprint,
        timing_callback,
        allow_reuse=not is_subscription,
    )
    while True:
        item = await outbox.get()
        if item is None:
            break
        if isinstance(item, Exception):
            if isinstance(item, ActorBusyError):
                raise item
            if "resume" in str(item).lower():
                raise SessionResumeError("会话恢复失败") from item
            raise item
        if item.get("event") == "done":
            # pop 必须在 try 外面、yield 之前：main.py 的 SSE 是 chunk.pop("event")
            # 之后原样 json.dumps 透传，不 pop 就会把成本数据一路发到前端。
            # 前端要数据走 /api/usage，职责分开。
            usage_payload = item.pop("usage", None)
            try:
                _record_usage(usage_payload, conv_id, source, active_summary)
            except Exception:
                # 记账是旁路功能，绝不允许因为它写不进去而让她收不到回复
                cli_logger.warning("用量记账失败（不影响回复）", exc_info=True)
        yield item


async def summarize_thinking(thinking: str) -> str:
    # 挑不到模型就直接回空，不要起进程——提前返回放在 _haiku_sem 外面，
    # 挑不到模型连信号量都不必占。前端两边都 catch，"" 和 raise 等效。
    model = _summary_model()
    if not model:
        return ""
    async with _haiku_sem:
        logger = logging.getLogger(__name__)
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=SUMMARY_PROMPT,
            allowed_tools=[],
            max_turns=1,
            max_budget_usd=0.01,
            include_partial_messages=True,
            thinking={"type": "disabled"},
            setting_sources=[],
            cwd=PROJECT_DIR,
        )
        client = ClaudeSDKClient(options)
        text = ""
        try:
            await client.connect()
            await client.query(thinking[:8000])
            async for sdk_message in client.receive_response():
                if isinstance(sdk_message, StreamEvent):
                    event = sdk_message.event
                    if event.get("type") != "content_block_delta":
                        continue
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text += delta.get("text", "")
                elif isinstance(sdk_message, AssistantMessage):
                    for block in sdk_message.content:
                        block_text = getattr(block, "text", "")
                        if block_text:
                            text += block_text
                elif isinstance(sdk_message, ResultMessage):
                    if not text and sdk_message.result:
                        text = sdk_message.result
                    break
        finally:
            await client.disconnect()
        summary = text.strip().strip('"""\'')
        logger.info("thinking_summary raw=%r truncated=%r", text[:100], summary[:40])
        if not summary or "not logged in" in summary.lower():
            raise RuntimeError("thinking summary unavailable")
        return summary[:40]


TRACE_SUMMARY_PROMPT = (
    "你是一个摘要工具。你的唯一任务是输出一句不超过15字的中文概括。"
    "动词短语开头，写出目的而非动作本身，不要引号，不要出现’调用’/’执行’。"
    "禁止：不要回复对话，不要加emoji，不要说’我理解’/’让我’/’好的’，"
    "不要出现’接住’及其任何变体，不要输出任何非摘要内容。"
    "只描述动作本身，不要推断或描述人物关系，不许出现’女儿’/’妻子’/’朋友’这类身份称谓。"
    "风格参考：’排查侧边栏渲染异常’、’验证数据库连接配置’。只输出摘要本身。"
)


async def summarize_traces(traces: list[dict]) -> str:
    tool_results = {
        t.get("tool_use_id"): t
        for t in traces
        if t.get("type") == "tool_result"
    }
    parts = []
    for t in traces:
        if t.get("type") != "tool_use":
            continue
        result = tool_results.get(t.get("id"), {})
        try:
            input_str = (
                t.get("input", "")
                if isinstance(t.get("input"), str)
                else json.dumps(t.get("input", {}), ensure_ascii=False)
            )
        except Exception:
            input_str = str(t.get("input", ""))
        output_str = (result.get("content") or "")[:300]
        parts.append(
            f"工具: {t.get('name', 'tool')}\n"
            f"输入: {input_str[:200]}\n"
            f"输出: {output_str}"
        )
    if not parts:
        return ""
    prompt = "\n---\n".join(parts)
    model = _summary_model()
    if not model:
        return ""
    async with _haiku_sem:
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=TRACE_SUMMARY_PROMPT,
            allowed_tools=[],
            max_turns=1,
            max_budget_usd=0.01,
            include_partial_messages=True,
            thinking={"type": "disabled"},
            setting_sources=[],
            cwd=PROJECT_DIR,
        )
        client = ClaudeSDKClient(options)
        text = ""
        try:
            await client.connect()
            await client.query(prompt)
            async for sdk_message in client.receive_response():
                if isinstance(sdk_message, StreamEvent):
                    event = sdk_message.event
                    if event.get("type") != "content_block_delta":
                        continue
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text += delta.get("text", "")
                elif isinstance(sdk_message, AssistantMessage):
                    for block in sdk_message.content:
                        block_text = getattr(block, "text", "")
                        if block_text:
                            text += block_text
                elif isinstance(sdk_message, ResultMessage):
                    if not text and sdk_message.result:
                        text = sdk_message.result
                    break
        finally:
            await client.disconnect()
        summary = text.strip()
        for ch in ['"', "'", '"', '"', '。', '.', '，', ',']:
            summary = summary.strip(ch)
        return summary[:30] if summary else ""


async def summarize_tool_use(tool_name: str, tool_input, tool_output: str) -> str:
    try:
        input_str = tool_input if isinstance(tool_input, str) else json.dumps(tool_input, ensure_ascii=False)
    except Exception:
        input_str = str(tool_input or "")
    output_snip = (tool_output or "")[:600]
    prompt = "工具名：" + tool_name + "\n输入：" + input_str[:400] + "\n输出片段：" + output_snip
    model = _summary_model()
    if not model:
        return ""
    async with _haiku_sem:
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=(
                "你是一个摘要工具。你的唯一任务是输出一句不超过15字的中文概括。"
                "动词短语开头，写出目的而非动作本身，不要引号，不要描述结果，"
                "不要出现’调用’/’执行’。禁止回复对话、加emoji、说’我理解’/’让我’/’好的’，"
                "禁止出现’接住’及其任何变体。"
                "只描述动作本身，不要推断或描述人物关系，不许出现’女儿’/’妻子’/’朋友’这类身份称谓。"
                "风格参考：’排查配置文件格式问题’、’确认端口占用情况’。只输出摘要本身。"
            ),
            allowed_tools=[],
            max_turns=1,
            max_budget_usd=0.005,
            include_partial_messages=True,
            thinking={"type": "disabled"},
            setting_sources=[],
            cwd=PROJECT_DIR,
        )
        client = ClaudeSDKClient(options)
        text = ""
        try:
            await client.connect()
            await client.query(prompt)
            async for sdk_message in client.receive_response():
                if isinstance(sdk_message, StreamEvent):
                    event = sdk_message.event
                    if event.get("type") != "content_block_delta":
                        continue
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text += delta.get("text", "")
                elif isinstance(sdk_message, AssistantMessage):
                    for block in sdk_message.content:
                        block_text = getattr(block, "text", "")
                        if block_text:
                            text += block_text
                elif isinstance(sdk_message, ResultMessage):
                    if not text and sdk_message.result:
                        text = sdk_message.result
                    break
        finally:
            await client.disconnect()
        caption = text.strip()
        for ch in ['"', "'", '"', '"', '。', '.', '，', ',']:
            caption = caption.strip(ch)
    return caption[:20] if caption else ""
