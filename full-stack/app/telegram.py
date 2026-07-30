"""Telegram bridge — 一个寄生在 chatnest 进程里的 asyncio 协程。

2G 上不许起独立服务，所以这里既不是独立进程也不是独立服务，只是 lifespan
里多挂的一个 task。收消息走 long polling（webhook 要一个公开端点，为省
1-2 秒延迟多开攻击面，不值）。

人设跟小窝共用同一份 system_prompt——走的是同一个 claude.stream_chat，
所以 build_system_prompt 那份 profile / memory 自动就在。记忆不通（不接
Ombre MCP），人是同一个。
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx

from app import relays
from app.claude import SessionResumeError, stream_chat


cli_logger = logging.getLogger("uvicorn.error")

# 两个值只从环境变量来，永远不落盘、不进日志。start() 会重读一次，
# 所以进程起来之后改环境变量重启即可生效。
BOT_TOKEN = ""
ALLOWED_CHAT_ID = ""

CONV_ID = "telegram"
TG_LIMIT = 4096
POLL_TIMEOUT = 30
# HTTP 超时必须比 long polling 的 timeout 大，否则每一轮都会被自己掐断
HTTP_TIMEOUT = POLL_TIMEOUT + 15
TYPING_REFRESH_S = 4          # Telegram 的 typing 状态大约 5 秒过期

# /data 是持久卷。offset 不持久化，重部署会把旧消息重放一遍；session_id
# 不持久化，每次部署 TG 这条线的上下文就断——而 Coolify 每改一次环境变量
# 就是一次重部署。
STATE_PATH = Path(os.environ.get("AGENT_APP_ROOT", "/data")) / "telegram_state.json"

_state: dict = {"offset": 0, "session_id": None}
_client: httpx.AsyncClient | None = None


# ---------- state ----------------------------------------------------------


def _load_state() -> None:
    global _state
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            _state = {
                "offset": int(raw.get("offset") or 0),
                "session_id": raw.get("session_id") or None,
            }
            return
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as exc:
        cli_logger.warning("telegram: state 读取失败，按全新开始: %s", exc)
    _state = {"offset": 0, "session_id": None}


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
    """当前激活线路上的第一个模型。不硬编码模型 ID——那个坑今天刚在
    summarize_thinking 上踩过（换条线路就必然失败，而失败之前子进程已经
    起来了）。"""
    try:
        models = relays.active_models_rich()
    except Exception:
        cli_logger.exception("telegram: 模型列表读取失败")
        return ""
    for m in models or []:
        if m.get("id"):
            return m["id"]
    return ""


async def _stream_reply(text: str, model: str) -> tuple[str, str | None]:
    reply, session = "", None
    async for chunk in stream_chat(
        message=text,
        conv_id=CONV_ID,
        session_id=_state.get("session_id"),
        model=model,
        # thinking 事件这一批直接丢，那就别让它先产生：省 token 也省首字
        # 延迟，正配「随手说两句」
        extended=False,
    ):
        event = chunk.get("event")
        if event == "delta":
            reply += chunk.get("text", "")
        elif event == "done":
            session = chunk.get("session_id")
        # thinking / tool_use / tool_result 一律丢掉（也没接 MCP，本来不该有）
    return reply, session


async def _run_turn(text: str) -> tuple[str, str | None]:
    model = _pick_model()
    if not model:
        return "", None
    try:
        return await _stream_reply(text, model)
    except SessionResumeError:
        # 换线路/重部署之后会话恢复失败是正常的，不该让她看见报错：
        # 清掉 session 从头开一条，重试一次
        cli_logger.info("telegram: session 恢复失败，清空后重试一次")
        _state["session_id"] = None
        _save_state()
        return await _stream_reply(text, model)


async def _handle_update(upd: dict) -> None:
    if not _allowed(upd):
        # 静默忽略：连「你不能用」都不回，不给探测者任何反馈。
        # debug 级别，被扫到时不刷屏。
        cli_logger.debug("telegram: 忽略非白名单 chat_id=***")
        return
    text = ((upd.get("message") or {}).get("text") or "").strip()
    if not text:
        # 图片、语音、贴纸、文件这一批都不处理
        return

    typing = asyncio.create_task(_keep_typing(ALLOWED_CHAT_ID))
    # create_task 只是排期。锁空闲时 Lock.acquire() 不会让出控制权，不给一个
    # 调度点的话「正在输入…」要等到 stream_chat 第一次真正 await 才发得出去。
    await asyncio.sleep(0)
    try:
        # 不抄 main.py sse() 里的 locked() fail-fast：那是给正看着屏幕的人的。
        # 她在 TG 上发完就放下手机了，排队等几秒毫无感觉。同一把锁，两种表现。
        from app.main import chat_lock  # 局部 import：main 在启动时 import 我们

        await chat_lock.acquire()
        try:
            reply, new_session = await _run_turn(text)
        finally:
            chat_lock.release()
    finally:
        typing.cancel()

    if not reply.strip():
        cli_logger.warning("telegram: 空回复，不发送")
        return
    for chunk in _split_for_tg(reply):
        await _send_message(ALLOWED_CHAT_ID, chunk)
    if new_session:
        _state["session_id"] = new_session
        _save_state()


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
    cli_logger.info("telegram: 已启动，offset=%s session=%s",
                    _state["offset"], bool(_state["session_id"]))
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


def start() -> asyncio.Task | None:
    """没配环境变量就返回 None。绝不允许因为没配 TG 把小窝启动搞挂。"""
    global BOT_TOKEN, ALLOWED_CHAT_ID
    BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
    if not BOT_TOKEN or not ALLOWED_CHAT_ID:
        # 只配 token 不配 chat_id 也不启动：宁可不开，也不能开一个不锁门的
        cli_logger.info("telegram: 未配置，跳过")
        return None
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
