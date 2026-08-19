"""凌晨守护：她半夜还在刷手机，梁忱从锁屏上敲她。

🔴 事件驱动，不是定时任务。手机上报 → 判断 → 推送，全程在 /api/events 那个
   请求里跑完。不动 lifespan、不起协程、不 setInterval。

🔴 说话的必须是**真的梁忱**：走 claude.stream_chat()，带完整人设和 Ombre 记忆。
   没有固定话术池，没有便宜模型代写。
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import claude, peek, push, store

# 借 uvicorn.error：全仓库没有 logging 配置，root logger 默认 WARNING 会把 .info() 吞掉
night_logger = logging.getLogger("uvicorn.error")

# 🔴 容器跑在 UTC，小朵在 UTC+8。直接拿容器的 hour 判断「凌晨 1 点」，实际会
#    落在她下午 5 点——漏了这条整块功能就是废的。
#    固定 +8，不用 zoneinfo / pytz：少一个依赖，也不受镜像里 tzdata 全不全的影响。
CN_TZ = timezone(timedelta(hours=8))


def _cn_hour() -> int:
    return datetime.now(CN_TZ).hour


def _cn_now_line() -> str:
    now = datetime.now(CN_TZ)
    return f"[现在是北京时间 {now:%Y-%m-%d %H:%M}]"


def _env_int(name: str, default: int) -> int:
    """环境变量都要有默认值，填了个非数字也不许崩。"""
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        night_logger.warning("[凌晨守护] %s 不是整数，退回默认值 %s", name, default)
        return default


EVENTS_TOKEN = os.environ.get("EVENTS_TOKEN", "")


def events_enabled() -> bool:
    return bool(EVENTS_TOKEN)


def _in_window(hour: int, start: int, end: int) -> bool:
    """🔴 支持跨零点。`start <= h < end` 配成 23→5 会算出空窗口然后**静默失效**，
    这种失败比报错坏得多。"""
    if start == end:
        return False  # 明确表示「关闭」，不要当成全天
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # 跨零点


def _in_night_window(hour: int) -> bool:
    # 小朵定的时段：0 点到 6 点。写成代码默认值，她不用在 Coolify 里填。
    return _in_window(hour, _env_int("NIGHT_GUARD_START", 0), _env_int("NIGHT_GUARD_END", 6))


def _still_scrolling() -> bool:
    """🔴 瞄一眼不算，持续刷才敲。

    她凌晨起夜点开微信看一眼消息就睡也会被敲——那不是守护，是扰民，
    她烦两次就会把快捷指令关掉。要求窗口内 app 类事件够多次才算「真的在刷」。

    数的是**去重之后**落库的事件（5 分钟去重在 /api/events 那头已经做了）。
    """
    minutes = _env_int("PEEK_WINDOW_MIN", 20)
    need = _env_int("NIGHT_GUARD_MIN_EVENTS", 2)
    if need <= 1:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    count = 0
    for row in store.recent_dream_events(max(1, (minutes // 60) + 1)):
        kind = str(row.get("type") or "")
        if not kind.startswith("app"):
            continue
        try:
            when = datetime.fromisoformat(str(row.get("created_at") or ""))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            count += 1
    return count >= need


def _cooled_down() -> bool:
    """距上次开口够不够久。读不出来就当「可以开口」——宁可多说一句，不要哑掉。"""
    minutes = _env_int("NIGHT_GUARD_COOLDOWN_MIN", 30)
    last = store.last_dream_event("_night_bark")
    if not last:
        return True
    try:
        when = datetime.fromisoformat(str(last.get("created_at") or ""))
    except ValueError:
        night_logger.warning("[凌晨守护] 上次开口时间解析不了，当作已冷却")
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when >= timedelta(minutes=minutes)


def _activity_block(hours: int = 6) -> str:
    """最近 6 小时手机上报的动静。内部的 _night_bark 不算「她在动」，滤掉。"""
    rows = [
        row
        for row in store.recent_dream_events(hours)
        if not str(row.get("type") or "").startswith("_")
    ]
    if not rows:
        return f"【最近 {hours} 小时她在动什么（手机自动上报）】\n（没有上报记录）"
    lines = []
    for row in rows:
        try:
            when = datetime.fromisoformat(str(row.get("created_at") or ""))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            stamp = when.astimezone(CN_TZ).strftime("%H:%M")
        except ValueError:
            stamp = "--:--"
        name = str(row.get("type") or "?")
        value = str(row.get("value") or "")
        lines.append(f"{stamp} {name}" + (f" {value}" if value and value != "open" else ""))
    return f"【最近 {hours} 小时她在动什么（手机自动上报）】\n" + "\n".join(lines)


def _recent_talk_block(conv_id: str, limit: int = 10) -> str:
    """最近 10 条对话原文。

    conversation_messages 返回的是 (messages, has_more, next_before_id) 三元组，
    带 limit 时已经按时间正序排好了。
    """
    try:
        messages, _has_more, _next = store.conversation_messages(conv_id, limit=limit)
    except store.ConversationNotFound:
        return "【最近的对话】\n（没有）"
    lines = []
    for item in messages:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        who = "她" if item.get("role") == "user" else "我"
        lines.append(f"{who}：{text}")
    if not lines:
        return "【最近的对话】\n（没有）"
    return "【最近的对话】\n" + "\n".join(lines)


def _build_message(conv_id: str, shot: Path | None = None) -> str:
    """🔴 动态数据全部挂用户消息侧，一个字都不许进 build_system_prompt()。

    system prompt 是稳定前缀，往里塞时间/活动就是每次都把她的缓存打掉——
    缓存前缀稳定化那一单的教训，不重复第二遍。
    """
    # 🔴 传本地绝对路径 + 「请用 Read 工具看一眼」+ 一句这是什么，不传二进制。
    #    照 main.py 频谱图那套抄的，先 exists() 再拼。Read 已经在 allowed_tools 里
    #    （claude.py:504/506），不用改工具配置。
    screen_block = ""
    if shot is not None and Path(shot).exists():
        screen_block = (
            "[这是她此刻的手机屏幕，请用 Read 工具看一眼：\n"
            f"{Path(shot).resolve()}\n"
            "凌晨了，她还在这个页面上。]"
        )
    return "\n\n".join([part for part in [
        _cn_now_line(),
        _activity_block(6),
        _recent_talk_block(conv_id, 10),
        screen_block,
        (
            "【这条不是她发的，是系统在凌晨叫醒你】\n"
            "现在是凌晨，她还在刷手机。你要从锁屏上叫她去睡。\n"
            "一两句话，30 字以内。可以凶、可以哄、可以威胁，但必须是你自己的话。\n"
            "从下面这些里挑一个最自然的切入点，不要每次都从同一个切：\n"
            "(a) 活动记录里有沉迷信号（同一个 app 短时间反复开）就调侃或者心疼她\n"
            "(b) 最近对话里有没说完的话题就接上去\n"
            "(c) 都没有就凭此刻的时间感说一句想她\n"
            "判断这次不该开口，就只输出 [NO_ACTION]。"
        ),
    ] if part])


async def _speak(conv_id: str, shot: Path | None = None) -> str:
    """走真的梁忱。返回累积的正文。"""
    model = claude.background_model("NIGHT_GUARD_MODEL")
    if not model:
        night_logger.warning("[凌晨守护] 挑不到模型，这一次不开口")
        return ""
    night_logger.info("[凌晨守护] model=%s", model)
    text = ""
    session_id = None
    async for chunk in claude.stream_chat(
        message=_build_message(conv_id, shot),
        conv_id=conv_id,
        # 🔴 不要 resume：resume 会把整个会话上下文重放一遍，一次几万 token；
        #    拼最近 10 条只要几千。一晚可能触发两三次，差价是她的额度。
        session_id=None,
        # 🔴 不要写死模型 ID：见 claude.background_model 的注释。
        model=model,
        # 🔴 用量账本要能单独看见主动开口花了多少钱。额度是她的痛点，
        #    混进「网页」里等于把这笔账藏起来。
        source="nightguard",
        # 🔴 绝对不许改成 True。lean 是 TG 那条轻量线，人设里只有「你是谁」，
        #    一件「我们发生过什么」都没有。半夜锁屏上那一句是她判断
        #    「这是不是梁忱」的全部依据——省这点 token 换一句不像他的话，
        #    是这一砖唯一不可接受的失败。
        lean=False,
    ):
        event = chunk.get("event")
        if event == "delta":
            text += chunk.get("text", "")
        elif event == "done":
            session_id = chunk.get("session_id")
    # session 文件的回收这一砖不做，但要留下 id 方便以后按图索骥。
    night_logger.info("[凌晨守护] 开口完成 session=%s 字数=%d", session_id, len(text))
    return text.strip()


def _gate(chat_lock: asyncio.Lock, *, force: bool = False) -> tuple[str | None, str]:
    """要不要开口。返回 (跳过原因 或 None, conv_id)。

    **只做判断，不做任何耗时的事**——这一段跑在 /api/events 的请求里。

    force=True 是测试端点用的：绕过时段、冷却、持续刷这三道，但 chat_lock /
    push.configured() / 有没有订阅照留——那三道保护的是她，不是测试的方便。
    """
    if not force:
        if not _in_night_window(_cn_hour()):
            return "not_night", ""
        if not _cooled_down():
            return "cooldown", ""
        if not _still_scrolling():
            return "just_a_glance", ""
    # 🔴 只判断，**不 acquire**：她正在跟我说话，不许插嘴。
    if chat_lock.locked():
        return "chatting", ""
    if not push.configured():
        return "push_unconfigured", ""
    if not store.list_push_subscriptions():
        return "no_subscription", ""
    conv_id = store.latest_conversation_id()
    if not conv_id:
        return "no_conversation", ""
    return None, conv_id


async def _bark(conv_id: str) -> dict:
    """看一眼屏幕 → 开口 → 落库 → 推送。返回统计。调用方负责兜异常。"""
    result: dict = {"cn_hour": _cn_hour(), "spoke": False, "skipped": None,
                    "text": "", "pushed": None, "shot": None}
    # 🔴 拿不到截图就不拼，照常开口——窥屏绝不许卡住守护。
    shot = await peek.capture()
    result["shot"] = str(shot) if shot else None

    text = await _speak(conv_id, shot)
    # 判断这次不该开口：不推送、不落库（冷却已经在后台任务开头写过了）
    if not text or text == "[NO_ACTION]":
        result["skipped"] = "no_action"
        return result

    result["text"] = text
    source_id = f"nightguard:{datetime.now(timezone.utc).isoformat()}"
    try:
        store.save_nightguard_message(conv_id, text, source_id)
    except Exception:
        # 落库失败不该拦住推送——锁屏上那句才是这一砖的目的
        night_logger.exception("[凌晨守护] 落库失败（不影响推送）")

    result["pushed"] = await push.send_push(title="梁忱", body=text, url="/")
    result["spoke"] = True
    night_logger.info("[凌晨守护] 开口：%s", text)
    return result


async def _bark_background(conv_id: str) -> None:
    """扔进 create_task 的那个。

    🔴 create_task 抛出来的异常没有人接，静默丢失比报错更难查——
       整个包在 try/except + logger.exception 里。
    """
    try:
        # 🔴 冷却在**任务一开始**就写，不等开口成功：窥屏最长要等 45 秒，
        #    这段窗口里再来一次上报会并发开两次口。
        try:
            store.add_dream_event("_night_bark", "")
        except Exception:
            night_logger.exception("[凌晨守护] 冷却记录写不进去")
        await _bark(conv_id)
    except Exception:
        night_logger.exception("[凌晨守护] 后台开口失败")


def trigger_bark(chat_lock: asyncio.Lock) -> str | None:
    """/api/events 用的：判断留在请求里，开口扔后台，上报立刻返回。

    返回跳过原因；None 表示已经派了后台任务。**不抛。**

    🔴 为什么必须异步：stream_chat 本身几秒到几十秒，加上窥屏的邮件往返
       15-45 秒，同步做会把上报请求吊死。iOS 快捷指令的 URL 请求有超时，
       挂久了会失败甚至重试，而重试又会再触发一次上报。
    """
    try:
        reason, conv_id = _gate(chat_lock)
        if reason:
            return reason
        asyncio.create_task(_bark_background(conv_id))
        return None
    except Exception:
        night_logger.exception("[凌晨守护] 判断阶段失败（上报仍然成功）")
        return "error"


async def maybe_bark(chat_lock: asyncio.Lock, *, force: bool = False) -> dict:
    """测试端点用的同步路径：等开口跑完、把那句话带回来。**任何情况都不抛。**

    线上真实触发走 trigger_bark()，不走这儿。
    """
    result: dict = {"cn_hour": _cn_hour(), "spoke": False, "skipped": None,
                    "text": "", "pushed": None, "shot": None}
    try:
        reason, conv_id = _gate(chat_lock, force=force)
        if reason:
            result["skipped"] = reason
            return result
        result = await _bark(conv_id)
        if result.get("spoke"):
            try:
                store.add_dream_event("_night_bark", result.get("text") or "")
            except Exception:
                night_logger.exception("[凌晨守护] 冷却记录写不进去")
    except Exception:
        night_logger.exception("[凌晨守护] 开口失败（上报仍然成功）")
        result["skipped"] = "error"
    return result
