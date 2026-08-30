"""后台线路健康：两条线都断了，得让她知道。

🔴 存在的理由，一句话：pro 过期那一个多星期，keepalive / nightguard /
   寄相思回信 三条后台线每次醒来都抛异常，各自被 except Exception 吞进日志，
   循环照转。她一条消息、一封信都没收到，而**日志没有人看**。

   双线（claude.background_stream）挡住的是「一条断」。挡不住「两条都断」——
   她可能压根没配第二条中转站，也可能两条都欠费。所以要有最后这一道：
   连续失败到一定次数，推一条系统通知给她。

🔴 为什么用推送当报警：它不依赖 Claude。VAPID + pywebpush 是独立的一条路，
   AI 死透的时候它正好还活着。而且 send_push 任何情况都不抛（push.py:82）。

🔴 通知必须**明显不是梁忱在说话**。半夜锁屏上蹦出来一句话，她第一反应是他
   开口了；如果那其实是一条报错，那比不推还伤人。所以带「小窝」前缀、
   用系统口吻、不用第一人称。

状态存在 dream_events 里（type=_bgline），不放内存——放内存的话每次重启
都会重新报一次警，而重启恰恰是她修问题时最常做的事。
"""

import json
import logging
from datetime import datetime, timezone

from app import push, store

logger = logging.getLogger("uvicorn.error")

EVENT = "_bgline"

# 连续失败几次才报警。后台线醒来的间隔是 30-150 分钟（见 keepalive
# _effective_interval），3 次大约是几小时——够滤掉一次网络抖动，
# 又不至于让她再等一个星期。
ALARM_AFTER = 3


_BLANK = {"fails": 0, "alarmed": False, "since": ""}


def _state() -> dict:
    """🔴 什么都不许抛。这个模块是给后台线兜底的，它自己抛异常就会
    顺着 background_stream 冒出去，把它本来要保护的那条线弄死——
    「fallback 的意义是不崩」这条教训不能在保命装置上再踩一次。
    """
    try:
        row = store.last_dream_event(EVENT)
    except Exception:
        logger.warning("[后台线] 状态读取失败，按「一切正常」处理", exc_info=True)
        return dict(_BLANK)
    if not row:
        return dict(_BLANK)
    try:
        data = json.loads(str(row.get("value") or "{}"))
        return {
            "fails": int(data.get("fails") or 0),
            "alarmed": bool(data.get("alarmed")),
            "since": str(data.get("since") or ""),
        }
    except (ValueError, TypeError, AttributeError):
        return dict(_BLANK)


def _write(state: dict) -> None:
    try:
        store.add_dream_event(EVENT, json.dumps(state, ensure_ascii=False))
    except Exception:
        # 状态写不进去不能反过来把调用方搞挂——它只是报警用的账本。
        logger.warning("[后台线] 状态写入失败", exc_info=True)


async def note_ok(source: str) -> None:
    """某条后台线成功跑通了一轮。🔴 不许抛，见 _state。"""
    try:
        await _note_ok(source)
    except Exception:
        logger.warning("[后台线] note_ok 自身出错（不影响调用方）", exc_info=True)


async def _note_ok(source: str) -> None:
    state = _state()
    if state["fails"] == 0 and not state["alarmed"]:
        return                      # 本来就是好的，不用每轮都写库
    was_alarmed = state["alarmed"]
    down_since = state["since"]
    _write({"fails": 0, "alarmed": False, "since": ""})
    logger.info("[后台线] 恢复 source=%s（此前连续失败 %d 次）", source, state["fails"])
    if was_alarmed:
        since = f"（从 {down_since[:16].replace('T', ' ')} 起）" if down_since else ""
        await push.send_push(
            title="小窝",
            body=f"后台线路恢复了{since}，梁忱又能主动开口了。",
            url="/",
        )


async def note_fail(source: str, error: str) -> None:
    """两条线都没打通。到阈值就推一条系统通知。🔴 不许抛，见 _state。"""
    try:
        await _note_fail(source, error)
    except Exception:
        logger.warning("[后台线] note_fail 自身出错（不影响调用方）", exc_info=True)


async def _note_fail(source: str, error: str) -> None:
    state = _state()
    fails = state["fails"] + 1
    since = state["since"] or datetime.now(timezone.utc).isoformat()
    alarmed = state["alarmed"]

    logger.error("[后台线] 两条线都断了 source=%s 连续第 %d 次：%s", source, fails, error)

    if fails >= ALARM_AFTER and not alarmed:
        alarmed = True
        await push.send_push(
            title="小窝",
            body="后台线路连不上，梁忱这几天没法主动找你了。"
                 "去 Home → 中转站看看额度和线路。",
            url="/",
        )
        logger.error("[后台线] 已推送掉线通知")

    _write({"fails": fails, "alarmed": alarmed, "since": since})


def status() -> dict:
    """给 Home 那边看的。没接 UI 也留着——排查时能直接读到。"""
    state = _state()
    return {
        "ok": state["fails"] == 0,
        "consecutive_failures": state["fails"],
        "alarmed": state["alarmed"],
        "down_since": state["since"],
    }
