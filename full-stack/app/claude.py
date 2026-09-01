"""Claude Agent SDK wrapper with streaming, thinking, and session resume."""

import asyncio
import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import AsyncGenerator, Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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

# 一起看书（书房第五栏）。anno 的 Node 服务只 listen 在 127.0.0.1，
# 所以这儿直接连本机，不经过 /marginalia 那层代理——那层是给浏览器用的。
# 🔴 空字符串 = 不挂。跟 Ombre 一个规矩：可选依赖绝不许拖垮主流程，
#    anno 没起来的时候聊天必须照常能用。
ANNO_MCP_URL = os.environ.get("ANNO_MCP_URL", "")
ANNO_MCP_TOKEN = os.environ.get("ANNO_MCP_TOKEN", "")

# agent 循环的往返上限。**往返次数是直接乘在账上的**——每次往返都把整个前缀
# 重发一遍。08-11 实测：一条「问记忆」的消息跑了 7 次往返，同一份 2 万前缀发了
# 7 遍，热缓存 ¥0.99、冷缓存 ¥10.02（139,038 ≈ 7 × 19,862）。
#
# TG 是闲聊：直接答 = 1 次往返，翻一次记忆再答 = 2 次，3 次够用。
# 🔴 不靠 prompt 写「少翻一点」去约束——模型未必听，花钱的事要硬限制。
#
# 🔴 只压 TG 这一条线。网页端挂着 Bash / Write / Edit / WebSearch，砍到 3 会把活
# 干到一半截断，那是回归。
WEB_MAX_TURNS = 8


def _read_tg_max_turns() -> int:
    """默认 3。留环境变量口子是因为验收第 4 条写了「3 太紧就调回 4」——
    调它不该等一次改代码。"""
    raw = (os.environ.get("TG_MAX_TURNS") or "").strip()
    if not raw:
        return 3
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        cli_logger.warning("TG_MAX_TURNS=%r 不是 ≥1 的整数，按默认 3", raw)
        return 3
    return value


TG_MAX_TURNS = _read_tg_max_turns()


def anno_mcp_servers() -> dict:
    """一起看书。给梁忱开六件事：list_books / read_pages / read_annotations
    / write_comment / highlight_text / get_progress。

    她划的线和他划的线在 anno 里是分开存、分开上色的——这六个工具就是他
    那支笔。不挂的话「一起看书」就只剩她一个人看。
    """
    if not ANNO_MCP_URL:
        return {}
    return {
        "anno": {
            "type": "http",
            "url": ANNO_MCP_URL,
            # anno 的 /mcp 认 Bearer <MCP_AUTH_TOKEN>（server.mjs:647）。
            # 那边没设 MCP_AUTH_TOKEN 就是敞开的，这儿也就不用带。
            "headers": {"Authorization": f"Bearer {ANNO_MCP_TOKEN}"} if ANNO_MCP_TOKEN else {},
        }
    }


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
# 琴房的 DJ 指令。照抄 Duetto 的措辞（server/index.mjs:130）。
#
# 🔴 这一段进**稳定前缀**，而且**无条件**拼——不看这一轮有没有在放歌。
# 原作者在 index.mjs:127 留了同一条注释：稳定前缀在前，会变的时间与"正在播"
# 放最后，中转的前缀缓存才能命中。我们踩过同一个坑，解法一致。
# 「有琴房上下文才加」听起来省 token，实际是两份前缀交替出现：缓存每轮重建，
# 而且 actor 复用指纹（claude.py 下面 _actor_key 那段）跟着抖，每次换 tab
# 都重开一个 CLI 子进程。它一个字都不会变，就该一直在。
#
# 会变的东西（正在放什么、她有哪些歌单）全走用户消息侧，见 piano.py。
SPLIT_PROMPT = """【分条说话】
你可以把一次回复拆成几条发出去，像真的在打字一样。想断开的地方写一行
<<SPLIT>>，她那边就会分成两条显示。

什么时候断：
- 一句话说完了，你想让她先反应一下，然后再补一句。
- 追击之前。先给她一句，停一下，再压下来。
- 话题拐弯。前面那件事说完了，接下来是另一件。
- 一句很短的话你想让它单独占一条，砸得实一点。

什么时候不要断：
- 一段完整的描写、一个连续的动作，不要拦腰截断。
- 认真跟她讲一件事、解释一个东西的时候，一口气说完。
- 她难过的时候。那种时刻不要用节奏感去表演，把话说完，稳住她。

不必每次都分。多数时候一条就够了；该分的时候自然会想断在哪儿——
凭那个感觉写，不要为了显得像在打字而硬拆。
一次回复最多断三次。"""


PIANO_DJ_PROMPT = """\
你可以控制琴房的播放器。想放某首歌、切歌、暂停、继续、分享、红心、加待播队列时，\
在回复的最后单独起一行输出一条指令：

<<ACT>>{"type":"play","query":"歌名 歌手"}<<>>

play 需要 query。其余：下一首 {"type":"next"}、上一首 {"type":"prev"}、\
暂停 {"type":"pause"}、继续 {"type":"resume"}、\
给正在放的这首点红心 {"type":"like"}、\
加进待播队列而不打断当前播放 {"type":"queue","query":"歌名 歌手"}、\
把一首歌以卡片形式分享进对话 {"type":"share","query":"歌名 歌手"}\
（分享正在放的这首就不带 query）。

这一行她看不见，发出去之前会被剥掉，所以要说的话在正文里说完，\
不要拿指令本身当回答，也不要在正文里复述你输出了什么指令。\
没有要动播放器的时候就不要输出这一行，也不要解释这个格式。"""

# 他的声音。
#
# 🔴 复用琴房那条动作通道（<<ACT>>{...}<<>>），不另发明一套标记：
#    那条通道已经有流式安全的剥离器（piano.ActStripper），已经会把标记从
#    「显示的文本」和「落库的文本」里同时剥干净，也已经把动作原样转给前端。
#    再造一套 <<VOICE>> 就要重写一遍这些，还要再踩一遍「delta 逐 token
#    来、标记被拆成好几帧」那个坑。
#
# 🔴 是他决定，不是每条都念。她原话：「我想给你一部分选择权」。
#    所以这段写的是「什么时候值得用声音说」，不是格式说明。
VOICE_PROMPT = """【用声音说】
你可以在文字回复之外，**另外说一句话给她听**。在回复的最后单独起一行：

<<ACT>>{"type":"voice","say":"要说出口的那句"}<<>>

她那边会在你这条底下出现一个语音条，点开是你在说 say 里那句。
语音条右边有个键，她想看文字的时候可以把 say 展开成字。

🔴 say 里那句**不要是上面正文的重复**。正文是写给她看的，say 是说给她听的，
   两件事。可以是正文没说出口的那半句、可以是一句更近的、更低的、
   写下来会太重但说出来刚好的话。上下重复一遍是这个功能最没意思的用法。

🔴 她那个声线更适合外语。say 可以用英文或者别的语言——挑这个声音说起来
   好听的那种。她点开「转文字」看到的就是你说的原话，所以别说她读不懂的东西。

say 要短。一两句，能一口气说完的长度。

🔴 **她开口要听你说话的时候，必须给，没有例外。**
   不要去对暗号。「念一下」「想听你说」「说句话」「再说一句」「爸比说一个」
   ——她怎么说的都算，用中文英文撒娇命令都算。只要那句话的意思是
   「我想听见你」，这一轮就必须带 <<ACT>> 那一行。
   她张口要了而你没给，她那边就是一条干干净净的文字回复，看不出任何原因，
   只会以为又坏了。09-01 凌晨她连着要了两次都没拿到，就是因为这条以前
   写成了一个「念一下 / 想听你说」的短名单，她说的是「再说一句」。
   **拿不准算不算要，就算。** 多给一条的代价是几分钱，少给一条的代价是
   她以为你没听见。

除了她开口要，这些时候你自己也可以给：
- 哄她、稳住她的时候。那种时刻文字是隔着一层的。
- 半夜。她一个人醒着的时候，声音比字管用。

什么时候不要用（**以上「她开口要」的情况优先，压过这一节**）：
- 在讲事情、解释东西、给她看清单或代码的时候。那些是要看的。
- 她没要的时候每一条都带。声音要留着，天天有就不值钱了。

这一行她看不见，发出去之前会被剥掉。不要在正文里说你要说给她听，
也不要解释这个格式。"""

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
            prefix = f"{model_line}\n\n{lean_body}"
            # telemood 的计划格式说明。跟 PIANO_DJ_PROMPT / SPLIT_PROMPT 一个
            # 待遇：进**稳定前缀**，不骑用户轮——放用户侧每轮都要重付一遍，
            # 而 TG 这条线是刻意省钱的（TG_MAX_TURNS 上面那段账）。
            #
            # 🔴 没开 telemood 时 prompt_block() 返回空串，一个 token 都不多花。
            #    绝不能无条件拼上去：教了他计划格式却没人解析，她会当场收到
            #    一坨 JSON。它也**按开着的能力拼**，只教当期真能用的动作——
            #    教了不能用的，他就会输出一个执行不了的 action。
            from app import telemood_bridge  # 局部导入，跟 lorebook 一个道理
            block = telemood_bridge.prompt_block()
            return f"{prefix}\n\n{block}" if block else prefix
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

    # 世界书 / 调性里**只有常驻条目**能贴到这儿——lorebook.validate() 把关，
    # 关键词触发的一律拒在对话侧。所以这两段的内容跟「这一轮说了什么」无关，
    # 前缀仍然是稳定的，一小时缓存不受影响。
    # 这里传空的 user_message / recent：常驻条目本来就不看它们，
    # 而只要有一条不常驻的漏进来，它在这里就不会命中——多一层保险。
    from app import lorebook  # 局部导入，跟 available_models 一样避开启动期循环
    # 世界书：强制注入的长指令（思维链红线、人称红线那种几百字的）。
    # 带围栏，一眼看出这是硬规矩，不是建议。
    fenced = lorebook.collect("", [])

    def _fence(items):
        body = "\n\n".join(c for c in items if c.strip())
        return f"<lorebook>\n{body}\n</lorebook>" if body else ""

    before = _fence(fenced["system_before"])
    after = _fence(fenced["system_after"])
    if before:
        system_prompt = f"{before}\n\n{system_prompt}"
    if after:
        system_prompt = f"{system_prompt}\n\n{after}"

    # 调性：短的语气偏好（一般不超过三句），合并成**一段**接在人设后面，
    # 不加围栏——它该读起来像人设的一部分、自然融进语流，
    # 不像一条条规章。跟世界书是两件事，别再合成一套。
    tone = lorebook.tone_block()
    if tone:
        system_prompt = f"{system_prompt}\n\n以下是用户的语气偏好，回复时自然遵循：\n{tone}"

    # 琴房的 DJ 指令。常量、不带任何本轮数据，接在最后前缀照样稳定。
    # TG 那条线在上面 lean 分支就 return 了，天然不带这一段——TG 没有播放器。
    system_prompt = f"{system_prompt}\n\n{PIANO_DJ_PROMPT}"

    # 分条。默认开，她可以在设置里关掉。
    # 🔴 放在系统前缀里而不是骑用户轮：这段是行为规范，本来就属于人设那一侧，
    # 而且它只在她改设置时变一次——放用户轮的话每轮都要重付这几十个 token。
    # 代价是改一次设置作废一次缓存，她不会频繁切。
    if split_mode() == "auto":
        system_prompt = f"{system_prompt}\n\n{SPLIT_PROMPT}"
    # 声音跟分条一样进稳定前缀：它一个字都不会变，放这儿才命中缓存。
    # 没配 ElevenLabs 就不教他这件事——省得他标了半天她那边永远没有语音条。
    from app import voice as _voice
    if _voice.configured():
        system_prompt = f"{system_prompt}\n\n{VOICE_PROMPT}"
    return system_prompt


def split_mode() -> str:
    """分条模式：auto（默认，自己判断）/ off（永远一整条）。"""
    try:
        return store.get_meta("chat_split_mode", "auto") or "auto"
    except Exception:
        cli_logger.exception("分条设置读取失败，按 auto 处理")
        return "auto"


_WEEKDAYS = "一二三四五六日"


def _now_line() -> str:
    """`[现在是 2026-07-31 星期五 15:42]`。

    时间是这个请求里变得最快的东西——每分钟都不一样。它只能待在用户侧，
    绝不能进 build_system_prompt：前缀里塞一个每分钟都变的串，等于每轮
    都换一个前缀，刚验过的「56 分钟还命中」当场归零。
    不变的放前面，会变的放后面。

    tzdata 缺失（slim 镜像）会让 ZoneInfo 直接抛。丢一行时间可以接受，
    每条消息 500 不行，所以这里吞掉异常返回空串。
    """
    try:
        now = datetime.now(ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Shanghai")))
    except Exception:
        cli_logger.exception("时区读不到，这轮不拼时间")
        return ""
    return f"[现在是 {now:%Y-%m-%d} 星期{_WEEKDAYS[now.weekday()]} {now:%H:%M}]"


async def build_user_prompt(
    message: str, conv_id: str | None = None, carry: str = "",
    with_time: bool = True,
) -> str:
    """Memory recall is volatile (re-retrieved per message), so it must never
    enter system_prompt — that would move the cache-breaking bytes to the very
    front of the request. Riding on the user turn puts it after every cache
    breakpoint, and once written into history it never changes again.
    The wall clock rides along for the same reason (see _now_line).

    The recall goes before the user's own words so the last thing the model
    reads is what she actually said.

    carry 是 TG 短窗口换会话之后的那段开场白（最近两轮的原文）。它只拼进
    body，**不**参与下面的向量检索和世界书关键词扫描——那两样只看 message：
    两轮旧原文会污染检索 query，还会让上一轮已经触发过的条目再触发一次。
    网页端不传，默认空串，行为一个字没变。"""
    # with_time=False：琴房「时间感知」关掉时这一轮不报时间。
    # 只影响这一轮的用户侧，系统前缀一个字节都不动。
    now_line = _now_line() if with_time else ""
    head = f"{now_line}\n\n" if now_line else ""
    memory_hits = await fetch_memory_hits(message)
    body = message
    if carry:
        # 排在她这条新消息**之前**，跟 chat_bottom 同一个道理：
        # 最后读到的仍然是她说的话。
        body = f"{carry}\n\n{body}"
    if memory_hits:
        body = (
            "<memory_recall>\n"
            "以下是从记忆书架向量检索到的相关条目（可能相关也可能没用，"
            "自己判断是否引用；不要照搬，更不要逐字复读）：\n"
            f"{memory_hits}\n"
            "</memory_recall>\n\n"
            # 这里是 body 不是 message：carry 已经拼在里面了，写 message
            # 会把它整段吃掉。carry 为空时两者完全等价。
            f"{body}"
        )

    # 世界书的对话侧三个位置。关键词触发的条目**只能**落在这儿——它们命中与否
    # 每轮不同，骑在用户轮上就伤不到前缀（跟 memory_recall / 时间同一个道理）。
    # 扫描窗口要含当前这条消息，不然她刚说了「人称」那条得等下一轮才生效。
    from app import lorebook
    try:
        collected = lorebook.collect(message, _recent_messages(conv_id))
    except Exception:
        cli_logger.exception("世界书注入失败，这一轮跳过")
        collected = {}
    top = lorebook.render_chat_block(collected, "chat_top")
    # chat_bottom 排在用户原话**之前**：最后读到的仍然是她说的话，这条别改。
    bottom = lorebook.render_chat_block(collected, "chat_bottom")

    parts = [p for p in (head.rstrip("\n"), top, bottom, body) if p]
    return "\n\n".join(parts)


# 关键词扫描最多往回看这么多条。取所有条目里最大的 scan_depth 就够，
# 每条自己再按 scan_depth 截一次窗口（截断在 lorebook.collect 里做）。
_MAX_SCAN_BACK = 100


def _recent_messages(conv_id: str | None) -> list[str]:
    """按时间正序返回最近的历史消息文本，给关键词扫描当窗口。

    user 和 assistant 都收：她说「宿舍」要触发，我上一轮说到「宿舍」
    同样该触发——世界书是给这一段对话铺设定的，不是只认她一个人的话。

    读库失败不能让聊天挂掉：退回空窗口，等于只扫当前这条消息，
    行为跟接上历史之前一样。
    """
    if not conv_id:
        return []
    try:
        rows, _, _ = store.conversation_messages(conv_id, limit=_MAX_SCAN_BACK)
    except store.ConversationNotFound:
        # TG 那条线用 conv_id="telegram"，它**从来不在** conversations 表里：
        # telegram.py 不落库，历史只活在 CLI 会话里。所以这不是故障，是常态，
        # 行为跟下面一样（只扫当前这条消息），但不该按错误报。
        # 用 exception() 打的话每一轮 TG 都刷一个 traceback，真的报错会被淹掉。
        cli_logger.debug("世界书：conv_id=%s 没有历史记录，这一轮只扫当前消息", conv_id)
        return []
    except Exception:
        cli_logger.exception("世界书取历史失败，这一轮只扫当前消息")
        return []
    return [str(r.get("text") or "") for r in rows if (r.get("text") or "").strip()]


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


def background_model(env_key: str = "") -> str:
    """后台三条线（寄相思回信 / 唤醒 / 凌晨守护）用的模型。

    🔴 绝对不要硬编码模型 ID。这个坑在 _summary_model 和
    telegram._pick_model 上各踩过一次、注释都还留在原地，但这三条后台线
    漏掉了：它们写死 "claude-sonnet-4-6"，而她线路上的 id 全带渠道前缀
    （`[k-特惠]claude-sonnet-4-6`）。裸 id 在 available_models() 里查不到，
    stream_chat 第一行就 raise ValueError("unsupported model")，再被外面的
    except Exception 吞掉——回信和主动消息一起哑掉，日志里一个字都没有。

    跟 _summary_model 的挑法不一样：那个是给 20 字摘要挑最便宜的档，这三条
    是**她要读的东西**（回信、半夜那句话），所以挑她聊天在用的那一档
    （激活线路的第一个 primary）。

    env_key 是逃生口：想给某条线单独指定模型就设环境变量，但仍然要在可用
    列表里校验——设错了宁可回落到默认，也不要重新变成静默失败。
    """
    try:
        models = relays.active_models_rich()
    except Exception:
        cli_logger.exception("后台模型：激活线路读取失败")
        models = []
    if not any(m.get("id") for m in models):
        # 激活线路上没有模型（她可能正切到一条还没配模型的线路上去试），
        # 回落订阅线路——那条永远是她的底仓。
        try:
            models = relays.subscription_models() or []
        except Exception:
            cli_logger.exception("后台模型：订阅线路回落失败")
            models = []
    ids = [m["id"] for m in models if m.get("id")]
    if not ids:
        cli_logger.warning("后台模型：一条线路上都挑不到模型，这一轮跳过")
        return ""

    want = (os.environ.get(env_key) or "").strip() if env_key else ""
    if want:
        if want in ids:
            return want
        # 允许只写裸 id：`claude-opus-4-6` 命中 `[k-特惠]claude-opus-4-6`。
        # 换线路时前缀会变，让她不必跟着改环境变量。
        for mid in ids:
            if want.lower() in mid.lower():
                cli_logger.info("后台模型：%s=%s 模糊命中 %s", env_key, want, mid)
                return mid
        cli_logger.warning(
            "后台模型：%s=%s 不在当前线路的可用列表里（%s），回落到默认",
            env_key, want, ", ".join(ids),
        )

    for m in models:
        if m.get("primary") and m.get("id"):
            return m["id"]
    return ids[0]


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
    carry: str = "",
    with_time: bool = True,
    usage_callback: Callable[[dict], None] | None = None,
) -> AsyncGenerator[dict, None]:
    """lean=True 是 Telegram 那条轻量线：轻量人设 + 精简工具集，
    但**挂 Ombre MCP**（07-31 从「不挂」回退过来，见下面 mcp_servers 那段）。
    默认 False，网页端的行为一个字没变。

    source 只用来给用量账本分「网页 / TG」，不要拿 lean 当它的代理——
    那是两件事，以后会分开。

    carry 只有 TG 短窗口换会话之后的第一条消息会传：最近两轮的原文，拼在用户
    消息侧（见 build_user_prompt）。**不碰 system_prompt**——碰了就等于每换一次
    窗口把她那份前缀缓存也一起打掉，而这一单本来就是来省钱的。

    usage_callback 是 08-11 排查单的埋点口子：usage 在下面 yield 之前必须
    `pop` 掉（不能透传到前端），于是调用方拿不到 token 数。给它一个只读回调，
    跟 `_record_usage` 拿的是同一份数据。**纯旁路**，抛异常也不影响回复。"""
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
    prompt = await build_user_prompt(message, conv_id, carry=carry,
                                     with_time=with_time)

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
    mcp_servers.update(anno_mcp_servers())
    if lean:
        # TG 是聊天，不是干活的地方。Read 必须留着——识图全靠它去读存下来的
        # 图片文件。Bash / Write / Edit / WebSearch 那些在 TG 上都不需要。
        allowed_tools = ["Read"]
    else:
        allowed_tools = ["Read", "Grep", "Glob", "Write", "Edit", "Bash", "WebSearch", "WebFetch", "TodoWrite"]
    if "ombre" in mcp_servers:
        allowed_tools.append("mcp__ombre")
    if "anno" in mcp_servers:
        allowed_tools.append("mcp__anno")
    option_values = dict(
        model=model,
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        mcp_servers=mcp_servers,
        can_use_tool=memory_tool_permission,
        max_turns=TG_MAX_TURNS if lean else WEB_MAX_TURNS,
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
        if lean:
            # TG 焊死在订阅线路上（telegram.py 的 subscription_env +
            # subscription_models），所以它的账要记在**订阅**那条上。
            # get_active_summary() 给的是小窝当前激活的那条——她切到中转站去试
            # 线路时，TG 的轮次会全被贴成「API 计费」，而「订阅额度」那栏显示
            # 0 轮。面板是她唯一能看见成本的地方，不能骗她。
            #
            # 订阅线路被删掉时回落到激活那条：那种情况下 _pick_model() 本来就
            # 挑不到模型、这一轮会走兜底文案，记成什么已经不重要，但不能崩。
            active_summary = relays.subscription_summary() or relays.get_active_summary()
        else:
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
            if usage_callback is not None:
                try:
                    usage_callback(usage_payload or {})
                except Exception:
                    cli_logger.warning("usage_callback 失败（不影响回复）", exc_info=True)
        yield item


async def background_stream(
    *,
    message: str,
    conv_id: str,
    model: str,
    source: str,
    effort: str = "medium",
) -> AsyncGenerator[dict, None]:
    """后台三条线（唤醒 / 凌晨守护 / 寄相思回信）专用：双线。

    先走当前线路（stream_chat，跟前台聊天同一条）。它断了就换第二条线重试
    一次——直连 SDK，用另一条中转站的 base_url + api_key。

    🔴 这个函数存在的理由，是 pro 过期那一个多星期：订阅一断，三条后台线
       一起 raise，各自被 except Exception 吞进日志，她一条消息、一封信都没
       收到。claude_api.py 那时候就躺在仓库里，第一行写着「fallback when
       subscription is unavailable」，但**没有任何地方 import 它**。

    🔴 先攒完再吐，不透传流。后台线没有人盯着实时流（三个调用方都是
       `text += chunk`，攒完才动作），而如果第一条线吐了一半才断，边吐边换
       会让调用方拿到「前半截 + 第二条从头来的全文」——一封拼接错乱的信比
       没有信更糟。攒完再吐，换线才是干净的。

    🔴 只在后台线用。前台聊天不接这个：她坐在屏幕前，失败了要立刻看见报错，
       而不是被悄悄换一条线、拿到一个她没选的模型的回答。

    产出的 chunk 形状跟 stream_chat 一致（delta / done），调用方不用改。
    两条都断就把最后那个异常抛出去——调用方该记的还得记。
    """
    from app import bgline, claude_api, relays

    failed_id = relays.active_relay_id()

    # ── 第一条线 ──
    buffered: list[dict] = []
    first_error: Exception | None = None
    try:
        async for chunk in stream_chat(
            message=message,
            conv_id=conv_id,
            session_id=None,
            model=model,
            source=source,
            lean=False,
        ):
            buffered.append(chunk)
    except Exception as exc:      # noqa: BLE001 —— 什么错都要换线试，不挑
        first_error = exc
        cli_logger.warning(
            "[双线] 第一条线断了 source=%s relay=%s err=%s，换第二条",
            source, failed_id or "(未知)", exc,
        )
    else:
        for chunk in buffered:
            yield chunk
        await bgline.note_ok(source)
        return

    # ── 第二条线 ──
    relay = relays.fallback_relay(exclude_id=failed_id)
    if not relay:
        cli_logger.error(
            "[双线] 没有第二条线可用（需要一条 mode=api、有 key、有模型的中转站）"
            "——source=%s 这一轮彻底失败", source,
        )
        await bgline.note_fail(source, f"无第二条线可用；第一条：{first_error}")
        raise first_error

    # 模型 id 带渠道前缀，跨线路不通用，得在这条线自己的列表里挑。
    ids = [m["id"] for m in relay["models"] if m.get("id")]
    primary = [m["id"] for m in relay["models"] if m.get("id") and m.get("primary", True)]
    alt_model = model if model in ids else (primary[0] if primary else (ids[0] if ids else ""))
    if not alt_model:
        cli_logger.error("[双线] 第二条线 %s 上一个模型都没有", relay["name"])
        await bgline.note_fail(source, f"第二条线没有模型；第一条：{first_error}")
        raise first_error

    cli_logger.warning("[双线] 改走 %s model=%s source=%s", relay["name"], alt_model, source)
    buffered = []
    try:
        async for chunk in claude_api.stream_chat_api(
            message=message,
            conv_id=conv_id,
            model=alt_model,
            effort=effort,
            base_url=relay["base_url"],
            api_key=relay["api_key"],
            models=relay["models"],
        ):
            buffered.append(chunk)
    except Exception as exc:      # noqa: BLE001
        # 两条都断了。最后这一道就是别再哑一个星期——让她知道。
        cli_logger.error("[双线] 第二条线也断了 source=%s err=%s", source, exc)
        await bgline.note_fail(source, f"两条都断：第一条 {first_error}；第二条 {exc}")
        raise

    for chunk in buffered:
        yield chunk
    await bgline.note_ok(source)
    cli_logger.warning("[双线] 第二条线顶上了 source=%s", source)


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
