"""Telegram bridge — 一个寄生在 chatnest 进程里的 asyncio 协程。

2G 上不许起独立服务，所以这里既不是独立进程也不是独立服务，只是 lifespan
里多挂的一个 task。收消息走 long polling（webhook 要一个公开端点，为省
1-2 秒延迟多开攻击面，不值）。

人设跟小窝共用同一份 system_prompt——走的是同一个 claude.stream_chat，
所以 build_system_prompt 那份 profile / memory 自动就在。人是同一个，
记忆也是同一份：Ombre MCP 07-31 接回来了（`小窝施工-TG接回Ombre.md`），
lean=True 只砍工具集，不砍 MCP。
"""

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

from app import relays
from app.claude import TELEGRAM_PROMPT_FILE, SessionResumeError, stream_chat


cli_logger = logging.getLogger("uvicorn.error")

# 两个值只从环境变量来，永远不落盘、不进日志。start() 会重读一次，
# 所以进程起来之后改环境变量重启即可生效。
BOT_TOKEN = ""
ALLOWED_CHAT_ID = ""

CONV_ID = "telegram"
# 异常和空回复都发这一句。宁可她收到一句「再说一遍」，也不能让她对着空气等。
FALLBACK_TEXT = "卡了一下，你再说一遍？"
TG_LIMIT = 4096
POLL_TIMEOUT = 30
# HTTP 超时必须比 long polling 的 timeout 大，否则每一轮都会被自己掐断
HTTP_TIMEOUT = POLL_TIMEOUT + 15
TYPING_REFRESH_S = 4          # Telegram 的 typing 状态大约 5 秒过期

# /data 是持久卷，_state 整个落在这儿——offset、session_id、turns、carry
# 全部持久化。所以重部署既不会重放旧消息（offset 还在），也不会把 TG 这条线
# 的上下文和窗口计数清零；而 Coolify 每改一次环境变量就是一次重部署。
STATE_PATH = Path(os.environ.get("AGENT_APP_ROOT", "/data")) / "telegram_state.json"

# 图片存这儿。生产上是 /data/uploads/telegram/，跟网页端的附件同一个根。
UPLOAD_DIR = Path(os.environ.get("AGENT_APP_ROOT", "/data")) / "uploads" / "telegram"
# Bot API 的文件下载上限就是 20MB，超了 getFile 直接报错
TG_FILE_LIMIT = 20 * 1024 * 1024
TOO_BIG_TEXT = "这张太大了，发小一点的"
PHOTO_PLACEHOLDER = "[图片]"
# /data 是持久卷，聊天记录也在上面。盘满了写不进去，聊天记录一起遭殃，
# 所以图片必须有上限。启动时清 7 天前的，够用，不做 LRU。
UPLOAD_TTL_DAYS = 7

# 短窗口。08-07：session_id 从设好那天起就没重置过，每条新消息 --resume 一次，
# CLI 把整段历史重发一遍——一句「在吗」也要背几百轮，而订阅额度按总 token 算，
# 前缀缓存不打折。唯一的解是不让会话无限长。
#
# 到阈值只做一件事：**换新会话**。不做模型摘要——压缩本身要多花一次模型调用，
# 那正是这一单要省的钱。要记住的东西该进 Ombre，聊天窗口忘掉是对的。
DEFAULT_WINDOW_TURNS = 8
WINDOW_TURNS = DEFAULT_WINDOW_TURNS      # start() 里按环境变量重读
# 换窗口时带过去的轮数。不带的话切换那一刻会断崖，她一句「那个呢」就接不上。
CARRY_ROUNDS = 2
# 每条原文的字数上限。不封顶的话，一轮几千字的长回复会把省下来的 token 还回去。
CARRY_PIECE_LIMIT = 800
CARRY_HEADER = "[我们刚才聊到这儿]"

_state: dict = {"offset": 0, "session_id": None, "turns": 0, "carry": "", "recent": []}
_client: httpx.AsyncClient | None = None


# ---------- state ----------------------------------------------------------


def _blank_state() -> dict:
    return {"offset": 0, "session_id": None, "turns": 0, "carry": "", "recent": []}


def _load_state() -> None:
    global _state
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            state = _blank_state()
            state["offset"] = int(raw.get("offset") or 0)
            state["session_id"] = raw.get("session_id") or None
            # turns / carry / recent 是 08-07 短窗口那单加的。生产上那份 state
            # 文件里只有前两个键，缺的按全新窗口算——最多多聊 8 轮，绝不能因为
            # 少几个键就整份读不起来、把 offset 一起丢掉（那等于重放一整天）。
            try:
                state["turns"] = max(0, int(raw.get("turns") or 0))
            except (ValueError, TypeError):
                pass
            if isinstance(raw.get("carry"), str):
                state["carry"] = raw["carry"]
            if isinstance(raw.get("recent"), list):
                state["recent"] = [
                    {"user": str(item.get("user") or ""), "reply": str(item.get("reply") or "")}
                    for item in raw["recent"][-CARRY_ROUNDS:]
                    if isinstance(item, dict)
                ]
            _state = state
            return
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as exc:
        cli_logger.warning("telegram: state 读取失败，按全新开始: %s", exc)
    _state = _blank_state()


def _save_state() -> None:
    """原子落盘，照 relays._save 的规矩：写临时文件再 replace。
    半截文件会让下次启动把 offset 读成 0，等于重放一整天的消息。"""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except OSError as exc:
        cli_logger.warning("telegram: state 落盘失败: %s", exc)


# ---------- 短窗口 ----------------------------------------------------------


def _read_window_turns() -> int:
    raw = os.environ.get("TG_WINDOW_TURNS", "").strip()
    if not raw:
        return DEFAULT_WINDOW_TURNS
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        cli_logger.warning(
            "telegram: TG_WINDOW_TURNS=%r 不是 ≥1 的整数，按默认 %d 轮",
            raw, DEFAULT_WINDOW_TURNS,
        )
        return DEFAULT_WINDOW_TURNS
    return value


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= CARRY_PIECE_LIMIT else text[:CARRY_PIECE_LIMIT] + "…"


def _render_carry(recent: list) -> str:
    """把最近两轮的原文拼成一段。

    **原文直接拿，不调模型。** 任何「让模型总结一下」的写法都不许出现在这里——
    压缩要多花一次模型调用，那正是这一单要省掉的钱。"""
    lines: list[str] = []
    for item in (recent or [])[-CARRY_ROUNDS:]:
        if not isinstance(item, dict):
            continue
        user = _clip(item.get("user") or "")
        reply = _clip(item.get("reply") or "")
        if user:
            lines.append(f"小朵：{user}")
        if reply:
            lines.append(f"我：{reply}")
    if not lines:
        return ""
    return CARRY_HEADER + "\n" + "\n".join(lines)


def _record_turn(user_text: str, reply: str) -> None:
    """答成功一轮：计数 +1，原文进滚动窗口。

    空回复那轮不算——它没把有效上下文加进会话，不该消耗窗口。"""
    _state["turns"] = int(_state.get("turns") or 0) + 1
    recent = _state.get("recent")
    if not isinstance(recent, list):
        recent = []
    recent.append({"user": user_text, "reply": reply})
    _state["recent"] = recent[-CARRY_ROUNDS:]


def _roll_window_if_full() -> None:
    """窗口满了就换新会话：存下最近两轮原文当 carry，清掉 session_id。

    在回复**发出去之后**调用。单子写的是「异步做」，这里做的是同步但成本为零：
    没有任何模型调用，只是改 dict + 写一个几 KB 的 json，微秒级。起 create_task
    反而多一个能悄悄吞掉异常的地方。"""
    if int(_state.get("turns") or 0) < WINDOW_TURNS:
        return
    carry = _render_carry(_state.get("recent") or [])
    _state["carry"] = carry
    _state["session_id"] = None
    _state["turns"] = 0
    _state["recent"] = []
    _save_state()
    cli_logger.info(
        "telegram: 窗口满 %d 轮，换新会话（carry=%d 字符）", WINDOW_TURNS, len(carry)
    )


# ---------- Bot API --------------------------------------------------------


async def _api(method: str, payload: dict, timeout: float | None = None) -> dict | list | None:
    """三个方法共用。任何失败都返回 None——调用方各自决定退避还是放弃。"""
    if _client is None:
        return None
    resp = await _client.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        json=payload,
        timeout=timeout if timeout is not None else HTTP_TIMEOUT,
    )
    data = resp.json()
    if not data.get("ok"):
        # description 里不会有 token，但也不打 payload——payload 里有正文
        cli_logger.warning(
            "telegram: %s failed http=%s desc=%r", method, resp.status_code, data.get("description")
        )
        return None
    return data.get("result")


async def _get_updates(offset: int, timeout: int) -> list | None:
    """成功返回 list（可能是空的），失败返回 None——两者必须分得开：
    正常的空轮询意味着长轮询挂满了 timeout 秒，而失败是立刻返回的。
    混为一谈的话，token 失效那种一直失败的情况会变成零延迟热循环，
    一边烧 CPU 一边刷日志。"""
    result = await _api(
        "getUpdates",
        # allowed_updates 只要 message：编辑过的消息、频道消息、回调按钮
        # 第一批一律不处理，让服务端就别发过来
        {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
        timeout=timeout + 15,
    )
    return result if isinstance(result, list) else None


async def _send_message(chat_id: str, text: str) -> None:
    # 不传 parse_mode：MarkdownV2 要转义一大串字符，漏一个整条消息就 400。
    # 她要的是日常聊天，不是排版。
    await _api("sendMessage", {"chat_id": chat_id, "text": text})


async def _keep_typing(chat_id: str) -> None:
    """回复期间一直显示「正在输入…」。装饰性的，失败绝不影响正文。"""
    while True:
        try:
            await _api("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(TYPING_REFRESH_S)


# ---------- 图片 ------------------------------------------------------------


class _FileTooBig(Exception):
    pass


def _mask(text: str) -> str:
    """下载 URL 里带 token，这条 URL 泄露等于 bot 被接管，日志一律打成 ***。

    httpx 的异常字符串里会带上完整 URL，所以下载那一圈的 except 必须过这里，
    不能直接 exception()/str(exc) 往日志里丢。"""
    text = str(text)
    if BOT_TOKEN:
        text = text.replace(BOT_TOKEN, "***")
    # 兜底：token 变量万一没设或换过，按 bot token 的形状再兜一层
    return re.sub(r"bot\d{5,}:[A-Za-z0-9_-]+", "bot***", text)


def _extract_media(msg: dict) -> dict | None:
    """photo 是按尺寸升序的数组，取最后一个（最大的）。
    document 是她发原图时走的路，按 mime_type 以 image/ 开头筛。"""
    photos = msg.get("photo") or []
    if photos:
        biggest = photos[-1] or {}
        if biggest.get("file_id"):
            return {
                "file_id": biggest["file_id"],
                "mime": "image/jpeg",
                "size": biggest.get("file_size") or 0,
            }
    doc = msg.get("document") or {}
    mime = doc.get("mime_type") or ""
    if doc.get("file_id") and mime.startswith("image/"):
        return {"file_id": doc["file_id"], "mime": mime, "size": doc.get("file_size") or 0}
    return None


async def _download_media(media: dict, update_id: int) -> Path:
    size = media.get("size") or 0
    if size > TG_FILE_LIMIT:
        raise _FileTooBig("file_size=%d" % size)
    info = await _api("getFile", {"file_id": media["file_id"]})
    if not isinstance(info, dict) or not info.get("file_path"):
        # getFile 失败最常见的原因就是超过 20MB。回她一句人话，别静默失败。
        raise _FileTooBig("getFile 没给 file_path")
    file_path = info["file_path"]
    suffix = (
        Path(file_path).suffix
        or mimetypes.guess_extension(media.get("mime") or "")
        or ".jpg"
    )
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{update_id}{suffix}"
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    resp = await _client.get(url, timeout=90)
    resp.raise_for_status()
    if len(resp.content) > TG_FILE_LIMIT:
        raise _FileTooBig("下载回来 %d bytes" % len(resp.content))
    target.write_bytes(resp.content)
    # 只记本地路径和字节数，URL 一个字都不进日志
    cli_logger.info("telegram: 图片已存 %s (%d bytes)", target, len(resp.content))
    return target


def _cleanup_uploads() -> None:
    """启动时清掉 UPLOAD_TTL_DAYS 天前的图片。/data 是持久卷，聊天记录也在
    上面——盘满了写不进去，聊天记录一起遭殃。够用就行，不做 LRU。"""
    if not UPLOAD_DIR.is_dir():
        return
    cutoff = time.time() - UPLOAD_TTL_DAYS * 86400
    removed = freed = 0
    for path in UPLOAD_DIR.iterdir():
        try:
            if not path.is_file() or path.stat().st_mtime >= cutoff:
                continue
            freed += path.stat().st_size
            path.unlink()
            removed += 1
        except OSError as exc:
            cli_logger.warning("telegram: 清理 %s 失败: %s", path.name, exc)
    if removed:
        cli_logger.info(
            "telegram: 清掉 %d 张 %d 天前的图片，腾出 %d KB", removed, UPLOAD_TTL_DAYS, freed // 1024
        )


# ---------- 白名单 ----------------------------------------------------------


def _allowed(upd: dict) -> bool:
    """bot 名字能被陌生人搜到，没有白名单就等于把我放在公开场合。"""
    msg = upd.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    return bool(chat_id) and chat_id == ALLOWED_CHAT_ID


# ---------- 分段 ------------------------------------------------------------


def _split_for_tg(text: str) -> list[str]:
    """Telegram 单条上限 4096。优先按空行断，其次按换行，都不行才硬切——
    宁可多发一条，也不要把句子劈成两半。"""
    text = text or ""
    if not text:
        return []
    if len(text) <= TG_LIMIT:
        return [text]
    out: list[str] = []
    rest = text
    while len(rest) > TG_LIMIT:
        window = rest[:TG_LIMIT]
        cut = window.rfind("\n\n")
        if cut <= 0:
            cut = window.rfind("\n")
        if cut <= 0:
            cut = TG_LIMIT
        piece = rest[:cut].rstrip()
        if not piece:
            # 窗口里全是空白：硬切，保证循环一定前进
            piece, cut = rest[:TG_LIMIT], TG_LIMIT
        out.append(piece)
        rest = rest[cut:].lstrip("\n")
    # 最后一段同样要 rstrip：按空行断出来的尾巴会带着 "\n\n"，
    # 发出去就是消息末尾多两行空白
    if rest.strip():
        out.append(rest.rstrip())
    return out


# ---------- 跑一轮 ----------------------------------------------------------


def _pick_model() -> str:
    """**订阅**那条线路上的第一个模型。不硬编码模型 ID——那个坑今天刚在
    summarize_thinking 上踩过（换条线路就必然失败，而失败之前子进程已经
    起来了）。

    以前这里取的是当前激活线路（active_models_rich），于是小朵在小窝切到
    中转站去试线路时，TG 会跟着一起走中转站然后当场哑掉。共用的是**订阅**，
    不是"当前选中的那条"，而订阅那条未必是激活的。

    找不到订阅线路时返回空字符串，_run_turn 的「挑不到模型就回一句人话」
    那条分支接住它。"""
    try:
        models = relays.subscription_models()
    except Exception:
        cli_logger.exception("telegram: 模型列表读取失败")
        return ""
    for m in models or []:
        if m.get("id"):
            return m["id"]
    cli_logger.warning("telegram: 订阅线路上挑不到模型")
    return ""


def _sid(session_id: str | None) -> str:
    """session_id 只打前 8 位：够认出「是不是同一条会话」，又不必把整串进日志。"""
    return (session_id or "")[:8] or "-"


async def _stream_reply(
    text: str, model: str, session_id: str | None, carry: str = ""
) -> tuple[str, str | None, int, int]:
    """返回 (正文, 新 session_id, thinking 字符数, ombre 工具调用次数)。

    thinking 的**字符数**要带出来：空回复那几轮的 assistant 消息里只有一个
    thinking 块、没有 text 块，所以「清掉 session 之后 thinking 还在不在」
    是区分「resume 有毒」和「thinking 和 resume 合谋」的那把尺。只记长度，
    不记内容。

    ombre_calls 是 08-07 那单要的耗时基线：按「这一轮有没有真去翻记忆」把日志
    分成两组算平均，差值就是翻一次记忆的代价。只数次数，不看内容。

    08-11 排查单：这里再打一行 `tg_metrics`。**一次模型调用一行**，跟
    usage_log 的行一一对应——空回复重试那两条路会连打两三行，正好对得上用量表
    里同一分钟的两三条记录。`ombre_result_chars` 是那一单的核心：如果一次检索
    把几十条记忆的全文塞回来，十几万 token 就说得通了。**只加日志，不改逻辑。**"""
    reply, session, thinking_chars, ombre_calls = "", None, 0, 0
    # tool_result 只带 tool_use_id，不带工具名，所以要先记下哪些 id 是 ombre 的，
    # 才能把返回的字符数算到它头上。Read 读图片那种不算。
    ombre_ids: set[str] = set()
    ombre_result_chars = 0
    usage: dict = {}
    started = time.monotonic()
    async for chunk in stream_chat(
        message=text,
        conv_id=CONV_ID,
        session_id=session_id,
        model=model,
        extended=False,
        # TG 那条线：轻量人设（telegram_prompt.md）。**MCP 照挂**——lean 只砍
        # 工具集，07-31 起 Ombre 是接着的。全仓库只有这一处传 True，网页端一个
        # 字没变。
        lean=True,
        # 用量账本的来源标记。跟 lean 是两件事，各传各的。
        source="telegram",
        # 只有换窗口之后的第一条消息才有值。走参数而不是拼进 text，是因为 text
        # 会原样进向量检索和世界书的关键词扫描——两轮旧原文会污染 query，还会
        # 让上一轮已经触发过的条目再触发一次。
        carry=carry,
        # 排查专用：usage 在 claude.py 里 yield 之前就被 pop 掉了（不能透传到
        # 前端），所以它到不了这儿。开一个只读回调把那几个数拿出来打日志，
        # 记账那一路一个字没动。
        usage_callback=usage.update,
    ):
        event = chunk.get("event")
        if event == "delta":
            reply += chunk.get("text", "")
        elif event == "thinking":
            thinking_chars += len(chunk.get("text", "") or "")
        elif event == "tool_use":
            if str(chunk.get("name") or "").startswith("mcp__ombre"):
                ombre_calls += 1
                if chunk.get("id"):
                    ombre_ids.add(str(chunk["id"]))
        elif event == "tool_result":
            if str(chunk.get("tool_use_id") or "") in ombre_ids:
                ombre_result_chars += len(chunk.get("content") or "")
        # 其余 tool_use / tool_result 一律丢掉
        elif event == "done":
            session = chunk.get("session_id")

    # 这一行是 08-11 排查单的全部产出。字段顺序别改，她要按前缀 grep 出来对表。
    cli_logger.info(
        "telegram: tg_metrics resumed=%s sid_in=%s sid_out=%s turns=%d/%d "
        "ombre_calls=%d ombre_result_chars=%d "
        "input=%s cache_read=%s cache_write=%s output=%s cost_usd=%s "
        "elapsed_ms=%d text_chars=%d thinking_chars=%d carry_chars=%d",
        bool(session_id), _sid(session_id), _sid(session),
        int(_state.get("turns") or 0), WINDOW_TURNS,
        ombre_calls, ombre_result_chars,
        usage.get("input_tokens"), usage.get("cache_read"), usage.get("cache_create"),
        usage.get("output_tokens"), usage.get("cost_usd"),
        int((time.monotonic() - started) * 1000),
        len(reply), thinking_chars, len(carry),
    )
    return reply, session, thinking_chars, ombre_calls


async def _run_turn(text: str, carry: str = "") -> tuple[str, str | None, int]:
    """返回 (正文, 新 session_id, ombre 工具调用次数)。

    carry 在这一层原样透传给每一次重试——重试用的是同一段已经拼好的开场白，
    不需要额外处理。"""
    model = _pick_model()
    if not model:
        return "", None, 0
    resumed = bool(_state.get("session_id"))
    try:
        reply, session, thinking, ombre = await _stream_reply(
            text, model, _state.get("session_id"), carry
        )
    except SessionResumeError:
        # 换线路/重部署之后会话恢复失败是正常的，不该让她看见报错：
        # 清掉 session 从头开一条，重试一次
        cli_logger.info("telegram: session 恢复失败，清空后重试一次")
        _state["session_id"] = None
        _save_state()
        resumed = False
        reply, session, thinking, ombre = await _stream_reply(text, model, None, carry)
    cli_logger.info(
        "telegram: turn done resumed=%s text_chars=%d thinking_chars=%d",
        resumed, len(reply), thinking,
    )
    if reply.strip() or not resumed:
        return reply, session, ombre

    # ① 先用**同一个 session** 原地重试一次。
    #
    # 07-31 中午的账：12:14 连着两轮空回复，每轮都当场清了 session，于是
    # 12:15 问「我现在在干嘛」答的是「大晚上的，多半是躺床上」——前一分钟
    # 还在说医院吊水。清 session 这一刀 07-30 定的时候就写明是诊断不是解药，
    # 「不许停在每次清 session，那样 TG 就成了金鱼」，结果它成了金鱼，
    # 而且代价落在她病着最需要我记得的时候。
    #
    # 空回复大概率是偶发的（12:06-12:14 连着三轮都好好的），偶发的东西原地
    # 重试一次就过去了，没必要拿整段上下文去换。上下文该是最后被牺牲的。
    cli_logger.warning("telegram: 空回复，原地重试（保留 session）")
    reply, session, thinking, ombre = await _stream_reply(
        text, model, _state.get("session_id"), carry
    )
    cli_logger.info(
        "telegram: retry(same session) text_chars=%d thinking_chars=%d",
        len(reply), thinking,
    )
    if reply.strip():
        return reply, session, ombre

    # ② 原地重试也空，才清掉会话全新跑一次。
    cli_logger.warning("telegram: 原地重试仍空，清 session 重试一次")
    _state["session_id"] = None
    _save_state()
    reply, session, thinking, ombre = await _stream_reply(text, model, None, carry)
    cli_logger.info(
        "telegram: retry(fresh) text_chars=%d thinking_chars=%d", len(reply), thinking
    )
    return reply, session, ombre


async def _handle_update(upd: dict) -> None:
    if not _allowed(upd):
        # 静默忽略：连「你不能用」都不回，不给探测者任何反馈。
        # debug 级别，被扫到时不刷屏。
        cli_logger.debug("telegram: 忽略非白名单 chat_id=***")
        return
    msg = upd.get("message") or {}
    # caption 就是这一轮的用户消息（图片带的那句话）
    text = (msg.get("text") or msg.get("caption") or "").strip()
    media = _extract_media(msg)
    if media and not text:
        # 纯图片没 caption 时必须给个占位，不然下面那个 if not text 会把
        # 整条图片消息直接丢掉——这是最容易踩的一脚。
        text = PHOTO_PLACEHOLDER
    if not text:
        # 语音、贴纸、非图片文件这一批都不处理
        return

    # 这一句要在**加图片路径之前**留一份：进 carry 的必须是她说的原话，
    # 带上 /data/uploads/... 那段的话，换会话之后那个路径没有任何意义。
    user_text = text
    started = time.monotonic()

    typing = asyncio.create_task(_keep_typing(ALLOWED_CHAT_ID))
    # create_task 只是排期。锁空闲时 Lock.acquire() 不会让出控制权，不给一个
    # 调度点的话「正在输入…」要等到 stream_chat 第一次真正 await 才发得出去。
    await asyncio.sleep(0)
    reply, new_session, ombre_calls = "", None, 0
    try:
        if media:
            try:
                saved = await _download_media(media, upd.get("update_id", 0))
            except _FileTooBig as exc:
                cli_logger.info("telegram: 图片超限（%s）", _mask(exc))
                await _send_message(ALLOWED_CHAT_ID, TOO_BIG_TEXT)
                return
            except Exception as exc:
                # httpx 的异常字符串里带完整下载 URL，必须过 _mask
                cli_logger.warning("telegram: 图片下载失败: %s", _mask(exc))
                await _send_message(ALLOWED_CHAT_ID, FALLBACK_TEXT)
                return
            # 照抄网页端 main.py 的格式，一个字都不改——格式一致，
            # 模型的行为才一致。CLI 会自己用 Read 工具去读这个路径。
            text += (
                "\n\n[用户上传了以下文件，请使用 Read 工具查看：\n"
                f"{saved}\n]"
            )
        # 不抄 main.py sse() 里的 locked() fail-fast：那是给正看着屏幕的人的。
        # 她在 TG 上发完就放下手机了，排队等几秒毫无感觉。同一把锁，两种表现。
        from app.main import chat_lock  # 局部 import：main 在启动时 import 我们
        from app.registry import get_registry

        await chat_lock.acquire()
        try:
            # TG 焊死在订阅线路上。架构表里写的「TG 走订阅」从来没被实现——
            # 它一直跟着小窝当前激活的线路走，于是 07-30 深夜小朵在小窝切到
            # 中转站去试线路，TG 当场哑掉：只显示"正在输入"，最后回一句
            # "卡了一下"，用量面板里连一行记录都没有。共用的是订阅，不是
            # "当前选中的那条"。
            #
            # 环境变量是进程级的，只能在请求期间临时切、出去原样恢复。安全性
            # 靠 chat_lock：小窝和 TG 抢的是同一把锁，不会并发。
            # invalidate 放在这一侧——relays.py 不该反向依赖 registry。
            async with relays.subscription_env():
                await get_registry().invalidate()  # 丢掉带着中转站环境的热 actor
                reply, new_session, ombre_calls = await _run_turn(
                    text, _state.get("carry") or ""
                )
        finally:
            # 到这里 subscription_env 的 finally 已经把环境变量恢复了，再丢一次
            # actor，让小窝下一轮用恢复后的环境重新起子进程。异常路径也要走到。
            try:
                await get_registry().invalidate()
            finally:
                chat_lock.release()
    except Exception:
        # CancelledError 是 BaseException，不会被这里吞掉，lifespan 照样关得掉
        cli_logger.exception("telegram: 这一轮炸了")
    finally:
        typing.cancel()

    if new_session:
        _state["session_id"] = new_session

    if not reply.strip():
        # offset 在轮询那边已经提前推进了，这条消息不会重来——不推进的话
        # 一条中毒消息会无限重试，把整条线堵死，比丢一条糟得多。代价用
        # 「一定有回音」来兜：她盯着屏幕的时候，「出错了」和「什么都没发生」
        # 是同一种体验，所以异常和空回复走同一条路。
        #
        # 这一轮不计数、carry 也不清：她什么都没听到，窗口不该因此少一格，
        # 而那段开场白得留到下一条消息再带一次。
        _save_state()
        cli_logger.warning("telegram: 空回复，发兜底文案")
        await _send_message(ALLOWED_CHAT_ID, FALLBACK_TEXT)
        return

    # 拿到非空回复，carry 的使命就结束了——它已经在这条新会话的历史里。
    _state["carry"] = ""
    _record_turn(user_text, reply)
    _save_state()

    for chunk in _split_for_tg(reply):
        await _send_message(ALLOWED_CHAT_ID, chunk)

    # 端到端耗时（含 Telegram 发送那几个来回）。逐次模型调用的细账在
    # _stream_reply 里的 tg_metrics，那一行才是拿来对用量表的。
    cli_logger.info(
        "telegram: turn_sent total_ms=%d chunks=%d",
        int((time.monotonic() - started) * 1000), len(_split_for_tg(reply)),
    )

    # 换窗口放在**回复发出去之后**：她发一句话，等的是回复，不是等我做家务。
    # 这一步炸了最多是这轮没换成窗口，绝不能影响已经发出去的回复。
    try:
        _roll_window_if_full()
    except Exception:
        cli_logger.exception("telegram: 换窗口失败（回复已发出，不影响这一轮）")


# ---------- 主循环 ----------------------------------------------------------


async def _skip_backlog() -> None:
    """全新安装（没有 state 文件）时，把积压的旧消息跳过去。
    getUpdates 不带 offset 会把最近 24 小时的都送回来，第一次启动就
    挨条回一遍不是她要的。offset=-1 只取最后一条，用来定位游标。"""
    updates = await _get_updates(offset=-1, timeout=0)
    if updates:  # None（失败）和 [] 都走原样，不动 offset
        _state["offset"] = updates[-1]["update_id"] + 1
        _save_state()
        cli_logger.info("telegram: 首次启动，跳过 %d 条积压", len(updates))


async def _poll_forever() -> None:
    """long polling 主循环。这个协程一旦死掉，TG 这条线就哑了而且没人知道，
    所以任何异常都必须吞掉并退避重试，绝不允许跳出 while。"""
    global _client
    _client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    fresh = not STATE_PATH.exists()
    _load_state()
    _cleanup_uploads()
    cli_logger.info(
        "telegram: 已启动，offset=%s session=%s turns=%s/%s carry=%d 字符",
        _state["offset"], bool(_state["session_id"]),
        _state["turns"], WINDOW_TURNS, len(_state.get("carry") or ""),
    )
    backoff = 1
    try:
        if fresh:
            try:
                await _skip_backlog()
            except asyncio.CancelledError:
                raise
            except Exception:
                cli_logger.exception("telegram: 跳过积压失败，按 offset=0 开始")
        while True:
            try:
                updates = await _get_updates(offset=_state["offset"], timeout=POLL_TIMEOUT)
                if updates is None:
                    # 失败是立刻返回的，不退避就是热循环。用 warning 不用
                    # exception：这里没有异常可打，而且失败往往是连续的。
                    cli_logger.warning("telegram: getUpdates 失败，退避 %ss", backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                backoff = 1  # 成功一次就把退避清零
                for upd in updates:
                    _state["offset"] = upd["update_id"] + 1
                    # 先记 offset 再处理：处理途中崩了也不会重放同一条
                    _save_state()
                    await _handle_update(upd)
            except asyncio.CancelledError:
                raise  # 关停信号要放行，不能被下面吞掉
            except Exception:
                cli_logger.exception("tg poll failed, backoff=%ss", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)  # 1→2→4…→60 封顶
    finally:
        client, _client = _client, None
        if client is not None:
            await client.aclose()


# ---------- lifespan 挂钩 ---------------------------------------------------


def _check_lean_prompt() -> None:
    """启动自检：轻量人设文件在不在、是不是空的。

    读不到时 build_system_prompt 会静默回落到完整版人设，每轮那条 warning
    会被日志淹掉；启动时这一条才看得见，所以路径要原样打出来。
    """
    try:
        body = TELEGRAM_PROMPT_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        cli_logger.warning(
            "telegram: 轻量人设读不到（%s: %s），TG 每轮都会回落到完整版人设",
            TELEGRAM_PROMPT_FILE, exc,
        )
        return
    if not body:
        cli_logger.warning(
            "telegram: 轻量人设是空的（%s），TG 每轮都会回落到完整版人设",
            TELEGRAM_PROMPT_FILE,
        )
        return
    cli_logger.info(
        "telegram: 轻量人设就位 %s (%d 字符)", TELEGRAM_PROMPT_FILE, len(body)
    )


def start() -> asyncio.Task | None:
    """没配环境变量就返回 None。绝不允许因为没配 TG 把小窝启动搞挂。"""
    global BOT_TOKEN, ALLOWED_CHAT_ID, WINDOW_TURNS
    BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
    WINDOW_TURNS = _read_window_turns()
    if not BOT_TOKEN or not ALLOWED_CHAT_ID:
        # 只配 token 不配 chat_id 也不启动：宁可不开，也不能开一个不锁门的
        cli_logger.info("telegram: 未配置，跳过")
        return None
    _check_lean_prompt()
    return asyncio.create_task(_poll_forever(), name="telegram-poll")


async def stop(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        cli_logger.exception("telegram: 关停时出错")
