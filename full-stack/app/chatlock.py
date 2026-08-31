"""chat_lock 的薄封装：还是那一把进程级单锁，只是它现在说得出「谁拿的、拿了多久」。

🔴 不许把这把锁去掉，也不许改成「每个会话一把」。
`relays.subscription_env()` 靠 pop 掉三个进程级的 `ANTHROPIC_*` 让 CLI fallback
到 `~/.claude` 的订阅凭据——环境变量是**进程级**的，没法只对 TG 那条协程单独
unset。安全性完全靠「小窝和 TG 抢的是同一把锁，不会并发」这一条。见
`relays.py` 里 `subscription_env()` 上面那段红字。拆开它 = 网页端莫名其妙走了
订阅额度（或者反过来），那种 bug 比「发不出消息」难查十倍。

所以这一层**一个锁的语义都没改**：还是一把，还是全进程共用。加的只有两件事：
  1. 观测——谁拿的、什么时候拿的、上一次持有了多久、怎么结束的。
  2. 有限等待——`acquire_within()`，等不到才失败，而不是秒拒。

配套：`/api/keepalive/status` 把 `status()` 原样吐出去，Home 首页那一行读它。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from time import monotonic


# 这个项目自己不配 logging，root logger 停在 WARNING，.info() 哪儿都不去。
# uvicorn 配了自己的——借它，跟 actor.py / main.py 的做法一致。
lock_logger = logging.getLogger("uvicorn.error")

# 持有超过这个秒数就打一条 WARNING。她不看日志，所以这条不是给她看的，
# 是给「事后翻 Coolify 日志对时间」用的：她说几点几分卡住了，日志里得有一行对得上。
WARN_AFTER_SECONDS = 30


def _wait_seconds() -> float:
    """秒拒改成有限等待，等多久。

    3-5 秒这个量级的来历：她的重发绝大多数发生在 abort 之后的那一瞬间
    （index.html 里 `stopActiveResponseNow()` 之后 `setTimeout(r,0)`），
    而上一轮那时候往往正在收尾，等几秒就拿到了。等太久她会以为卡死——
    「转圈」和「报错」都比「等 30 秒再报错」好。

    留成环境变量是为了她真机上跑几天之后能调，不用改代码。
    """
    try:
        v = float(os.environ.get("CHAT_LOCK_WAIT_SECONDS", "4"))
    except ValueError:
        v = 0.0
    # 0 或者负数 = 退回秒拒，那是这一单要修的行为本身，不给这个选项。
    return v if v > 0 else 4.0


CHAT_LOCK_WAIT_SECONDS = _wait_seconds()


# 来源标签 → 她看得懂的名字。后端直接把中文一起吐出去，前端不再维护第二份映射
# （两份映射迟早分家，这个仓库已经在 CSS 上吃过一次双写的亏）。
_HOLDER_LABELS = {
    "web": "小窝",
    "tg": "TG",
    "?": "未知",
}


def holder_label(holder: str | None) -> str:
    if not holder:
        return ""
    return _HOLDER_LABELS.get(holder, holder)


class ChatLock:
    """一把 asyncio.Lock + 一本持有者流水账。

    `locked()` / `acquire()` / `release()` 跟裸 Lock 同名同义，
    keepalive.py 和 nightguard.py 那两个只读的调用点一个字都不用改。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._holder: str | None = None
        self._request_id: str = ""
        self._since: float = 0.0
        # 上一次是谁、持有了多久、怎么结束的。她说「刚才卡了一下」的时候，
        # 锁多半已经放了——只看 locked() 什么都看不到，得有这一条尸检记录。
        self._last: dict | None = None
        self._warn_task: asyncio.Task | None = None

    # ── 只读 ────────────────────────────────────────────────

    def locked(self) -> bool:
        return self._lock.locked()

    def held_seconds(self) -> float:
        if not self._lock.locked() or not self._since:
            return 0.0
        return monotonic() - self._since

    def status(self) -> dict:
        """塞进 /api/keepalive/status 的那一坨。只有标签和秒数，不含任何凭据。"""
        return {
            "locked": self.locked(),
            "holder": self._holder,
            "holder_label": holder_label(self._holder),
            "request_id": self._request_id or None,
            "held_seconds": round(self.held_seconds(), 1),
            "last": dict(self._last) if self._last else None,
        }

    # ── 加锁 / 解锁 ──────────────────────────────────────────

    async def acquire(self, holder: str = "?", request_id: str = "") -> bool:
        """无限等。TG 走这条：她在 TG 上发完就放下手机了，排队等多久都无所谓。"""
        await self._lock.acquire()
        self._mark_acquired(holder, request_id)
        return True

    async def acquire_within(
        self,
        timeout: float,
        holder: str = "?",
        request_id: str = "",
    ) -> bool:
        """带超时地等。拿到 True，超时 False。

        🔴 返回 False 时**没有**持有这把锁，调用方不许 release。

        取消安全：`asyncio.timeout` 取消的是 `Lock.acquire()`，CPython 3.12 起
        它自己会把「已经被交接过来的锁」还回去并唤醒下一个等待者；而 acquire
        返回到 `_mark_acquired` 之间没有 await，不存在「拿到了但没记上」的窗口。
        """
        try:
            async with asyncio.timeout(timeout):
                await self._lock.acquire()
        except TimeoutError:
            return False
        self._mark_acquired(holder, request_id)
        return True

    def release(self, outcome: str = "ok") -> None:
        held = self.held_seconds()
        self._last = {
            "holder": self._holder,
            "holder_label": holder_label(self._holder),
            "request_id": self._request_id or None,
            "held_seconds": round(held, 1),
            "outcome": outcome,
        }
        if held >= WARN_AFTER_SECONDS:
            lock_logger.warning(
                "chat_lock_released holder=%s request_id=%s held=%.1fs outcome=%s",
                self._holder,
                self._request_id,
                held,
                outcome,
            )
        self._holder = None
        self._request_id = ""
        self._since = 0.0
        if self._warn_task is not None:
            self._warn_task.cancel()
            self._warn_task = None
        self._lock.release()

    @contextlib.asynccontextmanager
    async def hold(self, holder: str = "?", request_id: str = ""):
        """`async with chat_lock.hold("web", rid):`——异常路径也一定 release。"""
        await self.acquire(holder, request_id)
        try:
            yield self
        except BaseException:
            self.release("exception")
            raise
        else:
            self.release("ok")

    # 万一哪天有人顺手写了 `async with chat_lock:`，别让它在运行时才炸。
    # 来源记成 "?"，status() 里一眼能看出是谁没写清楚。
    async def __aenter__(self) -> "ChatLock":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release("exception" if exc_type else "ok")

    # ── 内部 ────────────────────────────────────────────────

    def _mark_acquired(self, holder: str, request_id: str) -> None:
        self._holder = holder or "?"
        self._request_id = request_id or ""
        self._since = monotonic()
        if self._warn_task is not None:
            self._warn_task.cancel()
        self._warn_task = asyncio.create_task(
            self._warn_if_slow(self._holder, self._request_id),
            name="chat-lock-warn",
        )

    async def _warn_if_slow(self, holder: str, request_id: str) -> None:
        """持有超过 30 秒打一条 WARNING。

        fallback 的意义是不崩，不是不吭声（AGENTS.md 第 3 条）：这条日志 +
        Home 首页那一行状态，是「锁被谁占着」唯一的两个出口。
        """
        try:
            await asyncio.sleep(WARN_AFTER_SECONDS)
        except asyncio.CancelledError:
            return
        lock_logger.warning(
            "chat_lock_slow holder=%s request_id=%s held>=%ss "
            "（这段时间里她发的每一条都会被挡）",
            holder,
            request_id,
            WARN_AFTER_SECONDS,
        )


chat_lock = ChatLock()
