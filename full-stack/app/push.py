"""Web Push：把梁忱的话推到小朵的锁屏。

砖 1 只负责管道，调度与主动开口是砖 2。

🔴 没配 VAPID 环境变量时所有函数静默失效，Nepeta 照常跑——
   照 claude.py:64-65 OMBRE_MCP_URL 那套，可选依赖绝不许拖垮主流程。
"""

import asyncio
import json
import logging
import os
from typing import Any

from app import store

# 借 uvicorn.error：全仓库没有 logging 配置，root logger 默认 WARNING 会把 .info() 吞掉
push_logger = logging.getLogger("uvicorn.error")

VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "")


def configured() -> bool:
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


def _quiet(fn, *args) -> None:
    """订阅表的记账写不进去绝不允许影响推送本身——照 store.record_usage 那条注释。"""
    try:
        fn(*args)
    except Exception as exc:
        push_logger.warning("[push] 订阅表写入失败 %s", exc)


def _send_one(sub: dict[str, Any], payload: str) -> str:
    """推一条。返回 ok / gone / fail，**不抛**。

    gone = 推送服务明确说这个订阅死了（404/410），调用方负责删。
    """
    # 🔴 pywebpush 只在函数体内 import，不许放模块顶部。它拖着 requests +
    #    cryptography，顶部 import 会把常驻内存永久抬高，跟 librosa/matplotlib
    #    一个待遇（队列 38 正在查 Nepeta 从 37MiB 涨到 189MiB 那件事）。
    from pywebpush import WebPushException, webpush

    subject = VAPID_SUBJECT
    if not subject:
        # configured() 只看 public + private，所以 subject 可能是空的。VAPID 规范
        # 要求 sub claim，缺了 pywebpush 直接抛。这里兜一个默认值让管道能通，
        # 但**必须出声**——「fallback 的意义是不崩，不是不吭声」。
        push_logger.warning("[push] VAPID_SUBJECT 没配，临时用 mailto:nepeta@localhost")
        subject = "mailto:nepeta@localhost"

    try:
        webpush(
            subscription_info={
                "endpoint": sub.get("endpoint", ""),
                "keys": {
                    "p256dh": sub.get("p256dh", ""),
                    "auth": sub.get("auth", ""),
                },
            },
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": subject},
        )
    except WebPushException as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code in (404, 410):
            push_logger.info("[push] 订阅已死 code=%s，删掉", code)
            return "gone"
        push_logger.warning("[push] 发送失败 code=%s %s", code, exc)
        return "fail"
    except Exception as exc:
        push_logger.warning("[push] 发送异常 %s", exc)
        return "fail"
    return "ok"


async def send_push(title: str, body: str, url: str = "/") -> dict[str, Any]:
    """推给所有订阅。**任何情况都不抛**——推送失败绝不允许影响调用方。"""
    if not configured():
        push_logger.info("[push] 没配 VAPID，跳过：%s", title)
        return {"sent": 0, "skipped": "unconfigured"}

    try:
        subs = await asyncio.to_thread(store.list_push_subscriptions)
    except Exception as exc:
        push_logger.warning("[push] 读订阅表失败 %s", exc)
        return {"sent": 0, "removed": 0, "failed": 0}

    payload = json.dumps(
        {"title": title, "body": body, "url": url}, ensure_ascii=False
    )
    sent = removed = failed = 0
    for sub in subs:
        endpoint = sub.get("endpoint") or ""
        # 🔴 webpush() 是同步阻塞（走 requests），必须 to_thread 包起来。Nepeta 是
        #    单进程 uvicorn，直接在 async 路由里调会卡住整个事件循环——推一条卡一次，
        #    聊天跟着停。照 main.py 那处 asyncio.to_thread 的先例。
        try:
            result = await asyncio.to_thread(_send_one, sub, payload)
        except Exception as exc:
            push_logger.warning("[push] to_thread 异常 %s", exc)
            result = "fail"
        if result == "ok":
            sent += 1
            await asyncio.to_thread(_quiet, store.mark_push_ok, endpoint)
        elif result == "gone":
            removed += 1
            await asyncio.to_thread(_quiet, store.delete_push_subscription, endpoint)
        else:
            failed += 1
            await asyncio.to_thread(_quiet, store.mark_push_fail, endpoint)

    stats = {"sent": sent, "removed": removed, "failed": failed}
    push_logger.info("[push] %s → %s", title, stats)
    return stats
