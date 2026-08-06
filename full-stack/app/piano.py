"""琴房后端：把 Duetto 当引擎用的服务端代理。

Duetto 只负责三件事——网易云登录态、整首音频下载喂 Gemini 分析、按歌缓存。
**所有对话仍然走小窝自己那条线**（同一份 system prompt、同一条 Ombre、同一个人），
所以这里永远不代理 Duetto 的 `/api/chat`。

🔴 token 只活在这一层。Duetto 的 `/api/*` 和 `/ws` 全要 token
（`server/index.mjs:46-58`），它等价于琴房的钥匙——下发到前端就等于挂在公网上。
前端只跟 chatnest 说话，chatnest 拿着 token 去跟 Duetto 说话。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("uvicorn.error")

# 走 Docker 内网别名，不吃 Traefik、不过 Basic Auth、Duetto 重部署也不断线
# （照 Ombre 那个 `ombre` 别名的做法）。别名和端口都从环境变量读，不硬编码。
BASE_URL = os.environ.get("DUETTO_BASE_URL", "http://duetto:4183").rstrip("/")
TOKEN = os.environ.get("DUETTO_TOKEN", "").strip()

# 歌单/歌词这类要等网易云，给宽一点；分析和在场记录是纯读本地 SQLite，给窄的。
TIMEOUT = float(os.environ.get("DUETTO_TIMEOUT", "20"))
CONTEXT_TIMEOUT = float(os.environ.get("DUETTO_CONTEXT_TIMEOUT", "4"))

_client: httpx.AsyncClient | None = None


def configured() -> bool:
    return bool(TOKEN)


def startup_check() -> None:
    """启动时大声报，别等出事才在某一轮里静默 fallback。

    ROADMAP 那条教训：fallback 的意义是不崩，不是不吭声。正常也打一行 INFO，
    这样「它真的读到了」是可验证的，而不是只在出事时才有声音。
    """
    if not TOKEN:
        logger.warning(
            "[琴房] DUETTO_TOKEN 未配置 —— /api/piano/* 会全部返回 503，"
            "琴房 tab 拉不到歌单。去 Coolify 给 chatnest 加这个环境变量。"
        )
        return
    logger.info("[琴房] 引擎就位 %s (token ****%s)", BASE_URL, TOKEN[-4:])


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=TIMEOUT)
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class PianoError(RuntimeError):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


async def call(path: str, params: dict[str, Any] | None = None,
               *, timeout: float | None = None) -> dict[str, Any]:
    """打 Duetto 的一个 GET 接口。token 在这里挂上，不出这一层。"""
    if not TOKEN:
        raise PianoError("琴房引擎未配置（缺 DUETTO_TOKEN）", status=503)
    client = await _get_client()
    try:
        resp = await client.get(
            f"{BASE_URL}{path}",
            params={k: v for k, v in (params or {}).items() if v not in (None, "")},
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=timeout or TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise PianoError(f"琴房引擎连不上：{exc.__class__.__name__}") from exc
    if resp.status_code == 401:
        # token 过期或被换掉了。说清楚是哪一头的问题，别让它伪装成「歌单空的」。
        raise PianoError("琴房引擎拒绝了 token（401），DUETTO_TOKEN 需要重配", status=502)
    if resp.status_code >= 400:
        raise PianoError(f"琴房引擎返回 {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise PianoError("琴房引擎返回的不是 JSON") from exc


# ── 上下文注入：给「正在放什么」拼一段文本 ────────────────────────────────
# 🔴 这段的产物只准挂在用户消息侧，绝不许进 system prompt。
# 歌会换、进度每秒都在变，进了 system prompt 就是每轮改前缀，cache_read 直接归零。

def _fmt_sec(x: Any) -> str:
    try:
        n = max(0, int(float(x)))
    except (TypeError, ValueError):
        return "0:00"
    return f"{n // 60}:{n % 60:02d}"


async def now_playing_block(np: dict[str, Any]) -> str:
    """按当前播放信息拼一段注入文本；拼不出东西就返回空串。

    分析拿不到是正常的（这首歌还没被分析过）——空就不附加那一段，不要卡住等它。
    Duetto 那边分析一首歌要几十秒，第一次听没有是应该的，下次就有了。
    """
    title = str(np.get("title") or "").strip()
    if not title:
        return ""
    artist = str(np.get("artist") or "").strip()
    song_id = str(np.get("id") or "").strip()

    head = title + (f" — {artist}" if artist else "")
    lines = [f"[琴房 · 正在一起听]", head]

    dur = np.get("dur")
    if dur:
        lines.append(f"进度 {_fmt_sec(np.get('pos'))} / {_fmt_sec(dur)}")
    lyric = str(np.get("lyric") or "").strip()
    if lyric:
        lines.append(f"当前唱到：{lyric[:60]}")

    # 分析和在场记录都是纯读 Duetto 的本地库（index.mjs:199 / :200 只查表，
    # 不触发分析），所以随便调不烧钱。但仍然给短超时，拿不到就算了。
    if song_id.isdigit():
        try:
            data = await call("/api/song-analysis", {"id": song_id},
                              timeout=CONTEXT_TIMEOUT)
            text = str(data.get("text") or "").strip()
            if text:
                lines.append(f"\n[这首歌的听感 · 你认真听过，当背景别复述]\n{text[:1200]}")
            impression = str(data.get("impression") or "").strip()
            if impression:
                lines.append(f"\n[你们和这首歌的回忆]\n{impression[:600]}")
        except PianoError as exc:
            logger.info("[琴房] 分析拿不到，跳过注入：%s", exc)

        try:
            data = await call("/api/song-notes", {"id": song_id, "limit": 6},
                              timeout=CONTEXT_TIMEOUT)
            notes = data.get("notes") or []
            rendered = []
            for n in notes[-6:]:
                thought = str(n.get("thought") or "").strip()
                reply = str(n.get("reply") or "").strip()
                if not thought and not reply:
                    continue
                piece = "- "
                passage = str(n.get("passage") or "").strip()
                if passage:
                    piece += f"歌词「{passage[:40]}」 "
                if thought:
                    piece += f"她说：{thought[:120]} "
                if reply:
                    piece += f"你回：{reply[:120]}"
                rendered.append(piece.rstrip())
            if rendered:
                lines.append("\n[以前听这首时说过]\n" + "\n".join(rendered))
        except PianoError as exc:
            logger.info("[琴房] 在场记录拿不到，跳过注入：%s", exc)

    return "\n".join(lines)
