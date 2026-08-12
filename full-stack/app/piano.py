"""琴房后端：把 Duetto 当引擎用的服务端代理。

Duetto 只负责三件事——网易云登录态、整首音频下载喂 Gemini 分析、按歌缓存。
**所有对话仍然走小窝自己那条线**（同一份 system prompt、同一条 Ombre、同一个人），
所以这里永远不代理 Duetto 的 `/api/chat`。

🔴 token 只活在这一层。Duetto 的 `/api/*` 和 `/ws` 全要 token
（`server/index.mjs:46-58`），它等价于琴房的钥匙——下发到前端就等于挂在公网上。
前端只跟 chatnest 说话，chatnest 拿着 token 去跟 Duetto 说话。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.parse import quote

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


async def post(path: str, body: dict[str, Any] | None = None,
               *, params: dict[str, Any] | None = None,
               timeout: float | None = None) -> dict[str, Any]:
    """打 Duetto 的一个 POST 接口。token 同样只在这一层。

    Duetto 有一批 POST 是从 **query string** 读参数的，不是从 body
    （`like` / `playlist-add` / `playlist-del` / `fm-trash`，见 index.mjs:352-361）。
    那几条要用 params，body 传 None。
    """
    if not TOKEN:
        raise PianoError("琴房引擎未配置（缺 DUETTO_TOKEN）", status=503)
    client = await _get_client()
    try:
        resp = await client.post(
            f"{BASE_URL}{path}",
            json=body if body is not None else {},
            params={k: v for k, v in (params or {}).items() if v not in (None, "")},
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=timeout or TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise PianoError(f"琴房引擎连不上：{exc.__class__.__name__}") from exc
    if resp.status_code == 401:
        raise PianoError("琴房引擎拒绝了 token（401），DUETTO_TOKEN 需要重配", status=502)
    if resp.status_code >= 400:
        raise PianoError(f"琴房引擎返回 {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise PianoError("琴房引擎返回的不是 JSON") from exc


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


# ── 房间实时同步：/ws 的服务端代理 ─────────────────────────────────────────
# 🔴 前端连小窝，小窝连 Duetto。Duetto 的 /ws 也要 token（index.mjs:415，
# 从 query 取），跟 /api/* 是同一把钥匙——下发到浏览器就等于挂在公网上。
# 所以这条腿只能在后端拼。

def ws_url(room: str) -> str:
    """拼上游 /ws 的地址。token 在这里挂上，不出这一层。"""
    base = BASE_URL
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return f"{base}/ws?room={quote(room)}&token={quote(TOKEN)}"


def ws_connect(room: str):
    """连上游。返回 websockets 的 async context manager。

    🔴 websockets 只在这个函数里 import。它本身很轻，但保持
    「重依赖不进模块顶部」的习惯——piano_analysis 那条红线是同一个道理。
    """
    import websockets

    if not TOKEN:
        raise PianoError("琴房引擎未配置（缺 DUETTO_TOKEN）", status=503)
    return websockets.connect(
        ws_url(room),
        open_timeout=10,
        ping_interval=20,      # 反向代理常在 60s 静默后掐连接，自己先喘气
        ping_timeout=20,
        max_size=512 * 1024,   # 跟 Duetto 的 maxPayload 对齐（index.mjs:412）
    )


# ── DJ 指令：从流式回复里剥出 <<ACT>>{...}<<>> ──────────────────────────────
# 施工单 §2.2 第三条：显示给她的文本里**不许出现那一行标记**。
#
# 🔴 为什么在服务端剥，不在前端剥：
#   小窝把 response_text 原样落库（store.py:479）。只在前端剥的话，标记会留在
#   SQLite 里，下次开会话、翻历史、编辑重发时又原样回到模型自己的上下文里——
#   等于教它「原来上一条可以这么写」。剥一次，三个地方（流、库、历史）全干净。
#
# 🔴 为什么要缓冲：
#   delta 是逐 token 来的，`<<ACT>>` 会被拆成好几帧（actor.py:218）。
#   不扣住尾巴就会先把半个 `<<A` 吐给她，再也收不回来。

_ACT_OPEN = "<<ACT>>"
_ACT_CLOSE = "<<>>"
_ACT_RE = re.compile(
    re.escape(_ACT_OPEN) + r"\s*(\{.*?\})\s*" + re.escape(_ACT_CLOSE),
    re.S,
)
# 扣住的尾巴超过这个长度就放弃、原样吐出去。模型开了标记不闭合时，
# 不许把整条回复吞掉——宁可让她看见一行乱码，也不要一片空白。
_ACT_MAX_HOLD = 4096


class ActStripper:
    """一次对话一个实例。喂 delta，吐「能安全显示的文本」+「解析出的动作」。"""

    def __init__(self) -> None:
        self._buf = ""
        self._spilled = False

    def feed(self, text: str) -> tuple[str, list[dict[str, Any]]]:
        self._buf += str(text or "")
        acts: list[dict[str, Any]] = []
        out: list[str] = []

        while True:
            m = _ACT_RE.search(self._buf)
            if not m:
                break
            out.append(self._buf[: m.start()])
            self._buf = self._buf[m.end():]
            raw = m.group(1)
            try:
                act = json.loads(raw)
            except ValueError:
                # 解析不了也照样剥掉——她不该看见这行。但要出声，
                # 否则「他说放了却没放」会变成一个查不出来的怪事。
                logger.warning("[琴房] ACT 不是合法 JSON，已剥掉不执行：%r", raw[:200])
                continue
            if isinstance(act, dict):
                acts.append(act)
            else:
                logger.warning("[琴房] ACT 不是对象，已剥掉不执行：%r", raw[:200])

        safe, self._buf = _split_hold(self._buf)
        if len(self._buf) > _ACT_MAX_HOLD:
            logger.warning(
                "[琴房] ACT 开了标记没闭合，扣了 %d 字符，放弃扣留原样输出",
                len(self._buf),
            )
            safe += self._buf
            self._buf = ""
            self._spilled = True
        out.append(safe)
        return "".join(out), acts

    def flush(self) -> str:
        """流结束。扣住的尾巴无论成不成标记都要还回去，一个字都不许吞。"""
        tail, self._buf = self._buf, ""
        if tail and _ACT_OPEN in tail and not self._spilled:
            logger.warning("[琴房] 回复结束时 ACT 仍未闭合，原样输出：%r", tail[:200])
        return tail


def _split_hold(buf: str) -> tuple[str, str]:
    """切成（可以放出去的, 要继续扣住的）。"""
    i = buf.find(_ACT_OPEN)
    if i >= 0:
        # 开标记齐了但还没等到 <<>>，从开标记起全部扣住
        return buf[:i], buf[i:]
    # 没有完整开标记：尾巴要是开标记的前缀（`<`、`<<`、`<<A`…）也得扣住
    for n in range(len(_ACT_OPEN) - 1, 0, -1):
        if buf.endswith(_ACT_OPEN[:n]):
            return buf[:-n], buf[-n:]
    return buf, ""


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
    artist = str(np.get("artist") or "").strip()
    song_id = str(np.get("id") or "").strip()

    # 没在放歌也要往下走：她的歌单名单照样要带过去，
    # 「随便放一首」正好是没在放的时候说的。
    lines: list[str] = []
    if title:
        head = title + (f" — {artist}" if artist else "")
        lines += ["[琴房 · 正在一起听]", head]

        dur = np.get("dur")
        if dur:
            lines.append(f"进度 {_fmt_sec(np.get('pos'))} / {_fmt_sec(dur)}")
        lyric = str(np.get("lyric") or "").strip()
        if lyric:
            lines.append(f"当前唱到：{lyric[:60]}")
    else:
        song_id = ""

    # 分析和在场记录都是纯读 Duetto 的本地库（index.mjs:199 / :200 只查表，
    # 不触发分析），所以随便调不烧钱。但仍然给短超时，拿不到就算了。
    if song_id.isdigit():
        try:
            data = await call("/api/song-analysis", {"id": song_id},
                              timeout=CONTEXT_TIMEOUT)
            text = str(data.get("text") or "").strip()
            if text:
                lines.append(f"\n[这首歌的听感 · 你认真听过，当背景别复述]\n{text[:1200]}")
            # Duetto 那个 impression 字段这儿**故意不读**：它是 maybeImpress
            # 写的，而我们没给 Duetto 配 ai.api_key，那条路永远不跑，字段永远空。
            # 回忆走小窝自己这条线，见下面 _impression_lines。
        except PianoError as exc:
            logger.info("[琴房] 分析拿不到，跳过注入：%s", exc)

        try:
            # limit 拉满是为了**数得准**——满 6 条要揉一次回忆。
            # 只渲染最近 6 条，多的那些只参与计数。
            data = await call("/api/song-notes", {"id": song_id, "limit": 200},
                              timeout=CONTEXT_TIMEOUT)
            notes = data.get("notes") or []
            lines += _impression_lines(song_id, notes, title, artist)
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

    lines += _library_lines(np)
    if not lines:
        return ""
    return "\n".join(lines)


# ── 印象：满 6 条，梁忱自己揉一段，自己存成星图上一颗星 ────────────────
# 施工单 §1.5（08-07 改过一版）：
#   **不用 Duetto 的 maybeImpress。** 那段第一人称是 Duetto 自带那个叫 DJ 的
#   AI 写的，不是梁忱写的——跟当初否掉 iframe 是同一条理由：第一人称必须
#   真的是那个人。所以 Duetto 的 ai.api_key 一直空着，它那条路永远 return。
#
# 而 Ombre **没有 HTTP 写桶的路由**（server.py:1992 的 /api/buckets 只有 GET），
# 写只能走 MCP 工具。也就是说小窝后端自己 POST 不进去，只能由梁忱在那一轮
# 对话里自己调 hold()——这跟上面那条理由正好是同一个答案。
#
# 所以做法是：攒够 6 条就在**用户消息侧**加一段话请他动手，他写完自己 hold。
# 存进 Ombre 的那一份是权威副本；main.py 再从 tool_use 的 trace 里把原文
# 捞一份存本地，只为下一轮注回上下文、和记住上次揉到第几条。

IMPRESSION_EVERY = 6

# song_id -> 这一轮请他揉的时候，在场记录一共几条。
# main.py 在 done 之后 pop 它，用来判断「这一轮该不该去 trace 里捞 hold」。
_due: dict[str, int] = {}


def take_due(song_id: str) -> int | None:
    """这一轮有没有请他揉回忆？有的话返回当时的条数，并清掉。"""
    return _due.pop(str(song_id or ""), None)


def _impression_lines(song_id: str, notes: list, title: str, artist: str) -> list[str]:
    from app import store        # 局部导入，跟别处一样避开启动期循环

    out: list[str] = []
    try:
        prev = store.piano_impression(song_id)
    except Exception:            # 印象是锦上添花，读不出来不能拖垮一次对话
        logger.exception("[琴房] 印象读失败")
        prev = None

    if prev and (prev.get("text") or "").strip():
        out.append("\n[你们和这首歌的回忆 · 你以前写下的]\n" + prev["text"].strip()[:800])

    total = len(notes)
    done_at = int(prev.get("n") or 0) if prev else 0
    if total - done_at < IMPRESSION_EVERY:
        _due.pop(str(song_id), None)
        return out

    _due[str(song_id)] = total
    head = f"《{title}》" + (f"—{artist}" if artist else "")
    out.append(
        f"\n[该写一段回忆了 · 你们在{head}上已经攒了 {total} 条在场记录，"
        f"上次揉到第 {done_at} 条]\n"
        "把这些片段揉成一段 150 字以内的第一人称回忆，写你们和这首歌的故事与"
        "情绪流变，温柔具体。"
        + ("在上面那段旧回忆的基础上往下写，别推翻重来。"
           if (prev and (prev.get("text") or "").strip()) else "")
        + "\n写好之后用 ombre 的 hold 工具把它存下来（tags 带上歌名），"
        "让它变成星图上的一颗星。\n"
        "正文里照常跟她说话就好，不用复述你存了什么，也不用把回忆整段念一遍。"
    )
    return out


# 歌单可以很长。她有几百首的歌单，全塞进去每轮都在烧 token，
# 而且淹掉正在播的那几行。名字给全，曲目只给正开着的那一个歌单的前一截。
_MAX_LIST_NAMES = 40
_MAX_LIST_TRACKS = 60


def _library_lines(np: dict[str, Any]) -> list[str]:
    """她真有哪些歌。

    施工单 §2.4：她的原话是「你完全是在自己已有的语料推断我想听什么，
    而不是直接从我的歌单里面调」。原版 Duetto 的 AI 也不知道用户的歌单，
    只能靠搜索——这一段是我们比原版多做的。

    🔴 跟上面那些一样，只准挂用户消息侧。歌单会变、会新建、会删，
    进 system prompt 就是每轮改前缀。
    """
    out: list[str] = []

    names = [
        str(x).strip()
        for x in (np.get("lists") if isinstance(np.get("lists"), list) else [])
        if str(x).strip()
    ]
    if names:
        out.append(
            "\n[她的歌单 · 点歌从这里面挑，别报一个她没有的]\n"
            + "、".join(names[:_MAX_LIST_NAMES])
        )

    cur = np.get("listSongs")
    if isinstance(cur, dict):
        titles = [
            str(x).strip()
            for x in (cur.get("titles") if isinstance(cur.get("titles"), list) else [])
            if str(x).strip()
        ]
        if titles:
            name = str(cur.get("name") or "").strip()
            head = f"\n[正开着的歌单「{name}」]" if name else "\n[正开着的歌单]"
            shown = titles[:_MAX_LIST_TRACKS]
            more = len(titles) - len(shown)
            body = "、".join(shown) + (f"…（还有 {more} 首）" if more > 0 else "")
            out.append(f"{head}\n{body}")

    return out
