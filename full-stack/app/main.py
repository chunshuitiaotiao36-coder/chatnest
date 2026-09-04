import asyncio
import base64
import binascii
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from fastapi import (
    Depends, FastAPI, File, Form, Header, HTTPException, Query, Request,
    UploadFile, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from typing import Any
from starlette.formparsers import MultiPartParser

MultiPartParser.max_part_size = 60 * 1024 * 1024  # 与 uploads.py 的 MAX_FILE_BYTES 对齐

from app import anno, appicon, auth, backgrounds, faces, glance, keepalive, listen, lorebook, moments, murmur, nightguard, peek, piano, piano_analysis, push, relays, starmap, store, telegram, voice
from app.actor import ActorBusyError, _mem_kv
from app.chatlock import CHAT_LOCK_WAIT_SECONDS, chat_lock
from app.claude import (
    SessionResumeError,
    available_models,
    stream_chat,
    summarize_thinking,
    summarize_tool_use,
    summarize_traces,
)
from app.codex_api import stream_codex_chat
from app.memory import (
    MAX_MEMORY_CHARS,
    add_saved_memory,
    import_claude_export_memories,
    read_diary_entries,
    read_memory,
    read_profile,
    write_memory,
    write_profile,
)
from app.memory_search import recall as recall_memory
from app.sessions import (
    remove_session,
    session_list,
    session_messages,
    set_session_starred,
    set_session_title,
)
from app.registry import configure_registry, get_registry
from app.store import (
    ConversationNotFound,
    add_dream_event,
    begin_turn,
    create_letter,
    dream_event_exists,
    complete_turn,
    conversation_messages,
    get_letter,
    list_letters,
    mark_letter_read,
    save_piano_impression,
    save_push_subscription,
    ensure_conversation,
    initialize_store,
    prepare_edit_turn,
    prepare_retry_turn,
    resolve_conversation,
    restore_branch,
    list_replies,
    unlock_letter,
    usage_report,
)
from app.uploads import (
    remove_conversation_uploads,
    save_uploads,
    validated_attachments,
    validated_file,
)


logger = logging.getLogger(__name__)
timing_logger = logging.getLogger("uvicorn.error")
STATIC = ROOT / "static"
# chat_lock 从 app.chatlock 引进来（同一个对象，telegram.py 那个
# `from app.main import chat_lock` 照旧能用）。裸 asyncio.Lock 换成薄封装的
# 理由写在 chatlock.py 顶上：语义一个字没改，多的是「谁拿的、拿了多久」。
initialize_store()
TRACE_CONTENT_CHARS = 20_000


def _usd_to_cny() -> float:
    """人民币换算的汇率走环境变量，跟着 /api/usage 一起回给前端。
    写死在前端以后改不动。"""
    try:
        rate = float(os.environ.get("USD_TO_CNY", "7.2"))
    except ValueError:
        rate = 0.0
    return rate if rate > 0 else 7.2


def trace_content(value: Any) -> str:
    text = str(value or "")
    if len(text) <= TRACE_CONTENT_CHARS:
        return text
    return text[:TRACE_CONTENT_CHARS] + "\n\n[output truncated]"


@asynccontextmanager
async def lifespan(app: FastAPI):
    relays.initialize()  # must run before any Claude subprocess is spawned
    registry = configure_registry(os.environ.get("AGENT_APP_ROOT", str(ROOT)))
    await registry.start()
    # 寄生在本进程里的一个协程，不是独立服务。没配 TG 环境变量时返回 None，
    # 小窝照常跑。
    tg_task = telegram.start()
    # 琴房引擎（Duetto）只是个可选的外部依赖：没配就在启动日志里大声说一句，
    # 别等她点开琴房看见空歌单才去猜是哪儿断了。
    piano.startup_check()
    piano_analysis.startup_check()
    # ── 自动唤醒：服务端定时器 ──
    ka_task = asyncio.create_task(keepalive.keepalive_loop(chat_lock))
    # ── 朋友圈：自己一条轻量循环 ──
    # 不跟 keepalive 合并：那条是 30-150 分钟的粗粒度，评论要 3-10 分钟。
    # 这条 60 秒一跳，但没到期的时候只是一条 SQL，不起子进程。
    mo_task = asyncio.create_task(moments.moments_loop())
    try:
        yield
    finally:
        mo_task.cancel()
        ka_task.cancel()
        await piano.aclose()
        await murmur.aclose()
        await telegram.stop(tg_task)
        await registry.stop()


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

AUTH_MODE = os.environ.get("AUTH_MODE", "app").strip().lower()
if AUTH_MODE not in {"app", "both"}:
    logger.warning("Unsupported AUTH_MODE=%r; falling back to app", AUTH_MODE)
    AUTH_MODE = "app"
BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "")
BASIC_AUTH_PASSWORD = os.environ.get("BASIC_AUTH_PASSWORD", "")
OUTER_AUTH_COOKIE = "claude_outer_auth"
OUTER_BASIC_AUTH_ENABLED = AUTH_MODE == "both" and bool(BASIC_AUTH_USER and BASIC_AUTH_PASSWORD)


def outer_auth_token() -> str:
    return hmac.new(
        os.environ["CHAT_SECRET"].encode(),
        f"{BASIC_AUTH_USER}:{BASIC_AUTH_PASSWORD}:outer-v1".encode(),
        "sha256",
    ).hexdigest()


def basic_auth_ok(header: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.removeprefix("Basic ").strip()).decode()
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, sep, password = decoded.partition(":")
    return bool(sep) and hmac.compare_digest(user, BASIC_AUTH_USER) and hmac.compare_digest(password, BASIC_AUTH_PASSWORD)


def request_is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


@app.middleware("http")
async def outer_basic_auth(request: Request, call_next):
    public_paths = (
        "/health",
        "/marked.min.js",
        "/favicon.ico",
        # Service Worker 的更新检查请求不保证带 cookie，被 401 拦住会导致
        # 注册失败或者静默不更新。
        "/sw.js",
        # 手机快捷指令直接打这个，不带 cookie / basic auth。绕过外层是必须的，
        # **但函数内部自己校验 EVENTS_TOKEN**，见 events()。
        "/api/events",
        # 截图上传，同上：绕过外层，函数内自己校验 EVENTS_TOKEN。
        "/api/peek",
        "/static/manifest.webmanifest",
        "/static/css/typography-locked.css",
        "/static/design-system.css",
    )
    # 🔴 图标要免认证。iOS 抓 apple-touch-icon 的请求不保证带 cookie，
    #    被 401 拦掉就拿不到图，主屏上又变回那个首字母方块。
    #    图标不是敏感数据，放行整个前缀。
    if (not OUTER_BASIC_AUTH_ENABLED
            or request.url.path in public_paths
            or request.url.path.startswith("/icons/")):
        return await call_next(request)
    token = request.cookies.get(OUTER_AUTH_COOKIE, "")
    if token and hmac.compare_digest(token, outer_auth_token()):
        return await call_next(request)
    if not basic_auth_ok(request.headers.get("authorization", "")):
        return Response(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Agent App"'},
        )
    response = await call_next(request)
    response.set_cookie(
        OUTER_AUTH_COOKIE,
        outer_auth_token(),
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        secure=request_is_https(request),
        samesite="lax",
    )
    return response


def require_auth(authorization: str = Header(default="")) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="unauthorized")


class AuthBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class PushSubscribeBody(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)
    p256dh: str = Field(min_length=1, max_length=256)
    auth: str = Field(min_length=1, max_length=256)
    ua: str = Field(default="", max_length=512)


class ChatBody(BaseModel):
    message: str = Field(default="", max_length=20_000)
    conversation_id: str | None = Field(default=None, max_length=256)
    session_id: str | None = Field(default=None, max_length=256)
    edit_message_id: int | None = Field(default=None, ge=1)
    retry_message_id: int | None = Field(default=None, ge=1)
    model: str = Field(default="claude-sonnet-4-6", max_length=64)
    effort: str = Field(default="medium", max_length=16)
    extended: bool = True
    attachments: list[str] = Field(default_factory=list, max_length=10)
    # 琴房 tab 在放歌时带上来的「现在放的是什么」。只有琴房会送这个字段，
    # 别的 tab 一个字都不加。**它绝不进 system prompt**，见下面注入点的注释。
    piano: dict[str, Any] | None = None
    # 她按住麦克风说的那一句，hervoice 分析出来的语气。跟 piano 一个规矩：
    # 只有按麦克风那条路会送，**绝不进 system prompt**，也**不进她那条消息的
    # 显示文本**——显示出来就变成「系统在她旁边标注她的情绪」，感觉很差。
    voice: dict[str, Any] | None = None


class ToolCaptionBody(BaseModel):
    tool_name: str = Field(min_length=1, max_length=128)
    tool_input: Any = None
    tool_output: str = Field(default="", max_length=20000)


class MemoryBody(BaseModel):
    content: str = Field(max_length=MAX_MEMORY_CHARS)


class ProfileBody(BaseModel):
    fullName: str = Field(default="", max_length=200)
    nickname: str = Field(default="", max_length=200)
    savedMemories: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    preferences: dict[str, Any] = Field(default_factory=dict)
    claudeExportImport: dict[str, Any] = Field(default_factory=dict)
    updatedAt: int | None = None


class ThinkingSummaryBody(BaseModel):
    thinking: str = Field(min_length=1, max_length=50_000)


class RenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class StarBody(BaseModel):
    starred: bool


class RelayModelBody(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)
    # 前端保存时按 id 从原记录继承这三个再发回来。不在这里声明的话 pydantic
    # 会把它们丢掉，relays._normalize_model 再补成默认值——种子里设的
    # primary:false / desc 就是这么被抹平的。
    desc: str = Field(default="", max_length=500)
    thinking: str = Field(default="adaptive", max_length=20)
    primary: bool = True


class RelayCapabilitiesBody(BaseModel):
    streaming: bool = True
    cache_control: bool = True
    reasoning: bool = False


class RelayCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # empty allowed: a subscription relay has no URL and no key.
    # relays.create_relay enforces that api-mode has both.
    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=500)
    mode: str = Field(default="api", max_length=20)
    protocol: str = Field(default="openai-compatible", max_length=50)
    capabilities: RelayCapabilitiesBody = Field(default_factory=RelayCapabilitiesBody)
    models: list[RelayModelBody] = Field(default_factory=list, max_length=200)


class RelayUpdateBody(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    mode: str | None = Field(default=None, max_length=20)
    protocol: str | None = Field(default=None, max_length=50)
    capabilities: RelayCapabilitiesBody | None = None
    models: list[RelayModelBody] | None = Field(default=None, max_length=200)


class LorebookBody(BaseModel):
    """世界书 / 调性条目。字段校验（尤其那条缓存红线）在 lorebook.validate()，
    不在这儿——create 和 update 必须走同一份规则，写两遍迟早对不上。"""
    name: str = Field(default="", max_length=100)
    enabled: bool | None = None
    content: str = Field(default="", max_length=20000)
    always_on: bool | None = None
    keywords: list[str] | None = Field(default=None, max_length=100)
    use_regex: bool | None = None
    case_sensitive: bool | None = None
    scan_depth: int | None = Field(default=None, ge=1, le=100)
    position: str | None = Field(default=None, max_length=20)
    # depth 08-05 起不再使用（position 里没有 depth 了），保留字段只为兼容旧客户端
    depth: int | None = Field(default=None, ge=0, le=200)
    role: str | None = Field(default=None, max_length=20)
    priority: int | None = Field(default=None, ge=0, le=10000)
    kind: str | None = Field(default=None, max_length=10)


class RelayTestBody(BaseModel):
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=500)
    protocol: str = Field(default="openai-compatible", max_length=50)


class RelayModelsFetchBody(BaseModel):
    # 订阅线路没有地址，所以这里不能像 RelayTestBody 那样要求 min_length=1
    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=500)
    protocol: str = Field(default="openai-compatible", max_length=50)
    mode: str = Field(default="api", max_length=20)
    # 给了就用存档里那条的真 key，她编辑一个已存在的站时不用重新输密钥
    relay_id: str = Field(default="", max_length=64)


class LetterCreateBody(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    title: str = Field(default="", max_length=200)
    cover_text: str = Field(default="", max_length=200)
    locked: bool = False
    lock_type: str = Field(default="none", max_length=10)
    unlock_at: str | None = Field(default=None, max_length=64)
    password: str | None = Field(default=None, max_length=128)


class LetterUnlockBody(BaseModel):
    password: str = Field(default="", max_length=128)


class LetterReplyBody(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)


class MomentCreateBody(BaseModel):
    content: str = Field(min_length=1, max_length=2_000)


class MomentLikeBody(BaseModel):
    liked: bool = True


class MomentCommentBody(BaseModel):
    content: str = Field(min_length=1, max_length=2_000)


class BackgroundMaskBody(BaseModel):
    mask: float = Field(ge=0.0, le=0.9)


def render_context_prompt(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ""
    last = messages[-1]
    if len(messages) == 1 and last["role"] == "user":
        prompt = last["text"]
    else:
        lines = []
        for message in messages:
            role = "User" if message["role"] == "user" else "Assistant"
            text = (message.get("text") or "").strip()
            attachments = message.get("attachments") or []
            if attachments:
                paths = "\n".join(
                    item.get("path", "")
                    for item in attachments
                    if item.get("path")
                )
                if paths:
                    text = f"{text}\n[attachments]\n{paths}".strip()
            lines.append(f"{role}: {text}")
        prompt = (
            "<conversation-context>\n"
            + "\n\n".join(lines[:-1])
            + "\n</conversation-context>\n\n"
            "Please continue from the context above and answer only the final "
            "user message below. Do not repeat the previous transcript.\n\n"
            f"User: {last.get('text', '').strip()}"
        )
    attachments = last.get("attachments") or []
    paths = "\n".join(
        item.get("path", "")
        for item in attachments
        if item.get("path")
    )
    if paths:
        prompt += (
            "\n\n[用户上传了以下文件，请使用 Read 工具查看：\n"
            f"{paths}\n]"
        )
    return prompt


# 一起看书（书房第五栏）。anno 的阅读器前端 + 转发到它 Node 服务的 /api。
# 前缀必须是 /marginalia：anno 前端里写死的，见 app/anno.py 开头。
app.include_router(anno.router)
# 他的声音。合成 + 缓存都在服务端，见 app/voice.py 开头那段。
app.include_router(voice.router)
# 听她说话（hervoice）。跟上面正好反过来：那个是我说给她听，这个是她说给我听。
app.include_router(listen.router)
# 潮汐（Murmur 情绪引擎）。只读，见 app/murmur.py 开头那段——
# 它挂了只是潮汐那一页显示没连上，聊天一切照旧。
app.include_router(murmur.router)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        STATIC / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/sw.js")
async def sw_js() -> FileResponse:
    # 🔴 必须挂在根路径：Service Worker 的 scope 由它自己的 URL 决定，
    #    放 /static/sw.js 就只能控 /static/*，推送到不了主页面。
    return FileResponse(
        STATIC / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


# 主屏图标。她自己画的，存在 /data/icons（持久卷）。
# 🔴 白名单取尺寸，不要把 name 直接拼进 Path——那是目录穿越。
_ICON_NAMES = {f"nepeta-{s}.png": s for s in appicon.SIZES}


@app.get("/icons/{name}", include_in_schema=False)
async def app_icon(name: str) -> FileResponse:
    """主屏图标。

    还没上传的时候返回 404，head 里那个 SVG 兜底，行为跟加这套东西之前
    完全一样——不会因为半成品把图标搞坏。

    🔴 这个路由在外层认证的白名单里（见 outer_basic_auth）。iOS 抓
    apple-touch-icon 的请求不保证带 cookie，被 401 拦掉就又变回首字母方块。
    """
    size = _ICON_NAMES.get(name)
    if size is None:
        raise HTTPException(status_code=404, detail="no such icon")
    path = await asyncio.to_thread(appicon.file_path, size)
    if path is None:
        raise HTTPException(status_code=404, detail="icon not uploaded yet")
    return FileResponse(
        path,
        media_type="image/png",
        # 图标换得不频繁，但换了要能刷掉。一天足够，iOS 装到主屏之后
        # 那份是它自己的副本，本来也不会自动更新。
        headers={"Cache-Control": "public, max-age=86400"},
    )


class SignatureBody(BaseModel):
    text: str = Field(default="", max_length=faces.SIGNATURE_MAX)


@app.get("/api/faces", dependencies=[Depends(require_auth)])
async def faces_state() -> dict:
    """头像 / 朋友圈封面 / 个性签名的状态。一次拿齐。"""
    return await asyncio.to_thread(faces.get_state)


@app.post("/api/faces/{slot}", dependencies=[Depends(require_auth)])
async def faces_upload(slot: str, file: UploadFile = File(...)) -> dict:
    if not faces.valid_slot(slot):
        raise HTTPException(status_code=404, detail="unknown slot")
    if file.content_type not in faces.ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail="unsupported image type")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > faces.MAX_BYTES:
        raise HTTPException(status_code=413, detail="图太大了")
    return await asyncio.to_thread(faces.store, slot, data)


@app.delete("/api/faces/{slot}", dependencies=[Depends(require_auth)])
async def faces_clear(slot: str) -> dict:
    if not faces.valid_slot(slot):
        raise HTTPException(status_code=404, detail="unknown slot")
    return await asyncio.to_thread(faces.clear, slot)


class FaceShapeBody(BaseModel):
    shape: str = Field(pattern="^(circle|rounded|squircle)$")


@app.put("/api/faces/shape", dependencies=[Depends(require_auth)])
async def faces_shape(body: FaceShapeBody) -> dict:
    """头像形状。存服务端而不是 localStorage——换个设备打开该是同一个样子。"""
    return await asyncio.to_thread(faces.set_shape, body.shape)


class FaceAliasBody(BaseModel):
    slot: str = Field(pattern="^(xiaoduo|elian)$")
    text: str = Field(default="", max_length=faces.ALIAS_MAX)


@app.put("/api/faces/alias", dependencies=[Depends(require_auth)])
async def faces_alias(body: FaceAliasBody) -> dict:
    """备注。跟 shape/signature 一样存服务端：换个设备打开该是同一个称呼。"""
    return await asyncio.to_thread(faces.set_alias, body.slot, body.text)


@app.put("/api/faces/signature", dependencies=[Depends(require_auth)])
async def faces_signature(body: SignatureBody) -> dict:
    return await asyncio.to_thread(faces.set_signature, body.text)


# 🔴 保留 require_auth，理由跟 background_file 那条一样：外层 basic auth
#    只在 AUTH_MODE=both 时启用，默认模式下它是空转的，不能当访问控制。
#    前端 fetch 带 Bearer 取回后转 blob: URL 再用，不走 CSS url() 直连。
@app.get("/api/faces/file/{slot}", dependencies=[Depends(require_auth)])
async def faces_file(slot: str) -> FileResponse:
    if not faces.valid_slot(slot):
        raise HTTPException(status_code=404, detail="unknown slot")
    path = await asyncio.to_thread(faces.file_path, slot)
    if path is None:
        raise HTTPException(status_code=404, detail="not set")
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


class SplitModeBody(BaseModel):
    mode: str = Field(pattern="^(auto|off)$")


@app.get("/api/chat/split-mode", dependencies=[Depends(require_auth)])
async def split_mode_get() -> dict:
    from app import claude
    return {"mode": await asyncio.to_thread(claude.split_mode)}


@app.put("/api/chat/split-mode", dependencies=[Depends(require_auth)])
async def split_mode_put(body: SplitModeBody) -> dict:
    """分条开关。

    🔴 这个设置进的是系统前缀（见 build_system_prompt），改一次会作废一次
    prompt cache。所以它该是个「设一次就不动」的开关，不要做成每轮可切的
    快捷按钮——那样每条消息都要重付一次前缀钱。
    """
    await asyncio.to_thread(store.set_meta, "chat_split_mode", body.mode)
    return {"mode": body.mode}


@app.get("/api/appicon", dependencies=[Depends(require_auth)])
async def appicon_state() -> dict:
    return await asyncio.to_thread(appicon.get_state)


@app.post("/api/appicon", dependencies=[Depends(require_auth)])
async def appicon_upload(file: UploadFile = File(...)) -> dict:
    """她在手机上传图标。前端已经切好尺寸并转成 PNG。

    一次只收一张：前端按 appicon.SIZES 逐个传，失败一个不影响其他。
    """
    if file.content_type not in appicon.ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail="图标必须是 PNG")
    try:
        size = int(file.filename.rsplit("-", 1)[-1].split(".")[0])
    except (AttributeError, ValueError):
        raise HTTPException(status_code=400, detail="文件名要形如 nepeta-180.png")
    if not appicon.valid_size(size):
        raise HTTPException(status_code=400, detail=f"尺寸只接受 {appicon.SIZES}")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > appicon.MAX_BYTES:
        raise HTTPException(status_code=413, detail="图太大了")
    return await asyncio.to_thread(appicon.store, size, data)


@app.delete("/api/appicon", dependencies=[Depends(require_auth)])
async def appicon_clear() -> dict:
    return await asyncio.to_thread(appicon.clear)


@app.get("/static/manifest.webmanifest", include_in_schema=False)
async def manifest_webmanifest() -> Response:
    return Response(status_code=204)


@app.get("/static/css/typography-locked.css", include_in_schema=False)
async def typography_locked() -> Response:
    return Response(status_code=204)


@app.get("/marked.min.js")
async def marked_js() -> FileResponse:
    return FileResponse(
        STATIC / "marked.min.js",
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/vptest.html")
async def vptest() -> FileResponse:
    """视口测试页。不含小窝任何代码，只回答一件事：
    position:fixed;bottom:0 在这台机器上够不够得到屏幕底。
    导航栏那条 59px 缝定性用，定完可以删。"""
    return FileResponse(
        STATIC / "vptest.html",
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/static/design-system.css")
async def design_system_css() -> FileResponse:
    return FileResponse(
        STATIC / "design-system.css",
        media_type="text/css",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/static/fonts/{name}")
async def static_font(name: str) -> FileResponse:
    if not name.endswith(".woff2") or "/" in name or ".." in name:
        raise HTTPException(status_code=404)
    path = STATIC / "fonts" / name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path,
        media_type="font/woff2",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


def _set_auth_cookie(resp: Response, token: str, request: Request) -> Response:
    """把登录 token 种成 cookie。

    🔴 书房「一起看书」是嵌进来的 anno 阅读器（iframe），iframe 里发出的
       请求带不了 Authorization 头，只能靠 cookie 认。见 app/anno.py。
       HttpOnly：JS 读不到，少一条被 XSS 顺走的路；前端本来也不需要读它。
    🔴 登录和补种两条路都走这一个函数。分开写过一次，结果就是下面那个 bug：
       只有登录那条种了 cookie，早就登录过的人永远拿不到。
    """
    resp.set_cookie(
        anno.COOKIE_NAME,
        token,
        max_age=400 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=request_is_https(request),
        path="/",
    )
    return resp


@app.post("/api/auth")
async def login(body: AuthBody, request: Request) -> Response:
    token = auth.issue_token(body.password)
    if token is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return _set_auth_cookie(JSONResponse({"token": token}), token, request)


@app.post("/api/auth/cookie")
async def auth_cookie(request: Request, authorization: str = Header(default="")) -> Response:
    """拿手上已有的 Bearer token 换一个同内容的 cookie。

    🔴 为什么需要这条：cookie 原来只在 /api/auth 登录那一刻种。而她的 token
       一直躺在 localStorage 里，几个月没重新登录过——于是从来没拿到过
       cookie，点开「一起看书」是一整屏 unauthorized。让她退出重登能解决，
       但那是把维护成本推给她，而且 cookie 到期之后还会再犯。
    🔴 这条不放宽任何东西：要求一个**已经有效的** Bearer token，发出去的
       cookie 装的就是同一个 token，权限一模一样。没有 token 的人这里也
       什么都拿不到。
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="unauthorized")
    return _set_auth_cookie(JSONResponse({"ok": True}), token, request)


@app.post("/api/chat", dependencies=[Depends(require_auth)])
def _run_life_act(act: dict, conv_id: str) -> dict | None:
    """他在聊天途中自己决定发一条动态 / 写一封信。

    她 09-02 的原话：「你不知道自己在哪，有哪些事可以做，那我做的寄相思和
    朋友圈就都变成了让你表演给我看的作秀。」——这个函数就是那句话的答案：
    路一直在（keepalive.py 早就以 author="elian" 写过），只是聊天途中的他
    够不着。现在够得着了。

    🔴 分寸沿用 keepalive.py 定好的那套，一个字都不改：
       **都不推送。** 动态是她路过朋友圈才看见，信是她自己去书房开邮箱
       才看见。推了就变成他追上去说「我发了」，这个功能的意思就没了。

    🔴 绝不抛。这是他回话途中顺手做的一件事，写失败了也不能把她那条
       回复打断——跟潮汐注入一个规矩。

    返回给她的回执（前端画一行小字），失败或空内容返回 None。
    """
    kind = act.get("type")
    content = str(act.get("content") or "").strip()
    if not content:
        logger.warning("[生活] %s 但 content 为空，跳过", kind)
        return None
    title = str(act.get("title") or "").strip()
    try:
        if kind == "letter":
            new_id = store.create_letter(
                author="elian", content=content, title=title,
                cover_text="", locked=False,
            )
            logger.info("[生活] 他写了封信 id=%d conv=%s：%s", new_id, conv_id, content[:60])
            return {"kind": "letter", "id": new_id, "title": title}
        # moment
        # reply_status="none"：他自己发的动态不需要他自己回。
        # title 当 context_note 用——她看不见，是留给以后的他的线索，
        # 跟 keepalive 那边一致。
        new_id = store.create_moment(
            author="elian", content=content,
            context_note=title, reply_status="none",
        )
        logger.info("[生活] 他发了条动态 id=%d conv=%s：%s", new_id, conv_id, content[:60])
        return {"kind": "moment", "id": new_id}
    except Exception:  # noqa: BLE001
        logger.exception("[生活] %s 写入失败，这一轮当没发生", kind)
        return None


async def chat(body: ChatBody) -> StreamingResponse:
    request_id = uuid4().hex[:12]
    request_started = perf_counter()
    timing_stages = {"request_received"}

    def log_timing(stage: str) -> None:
        if stage in timing_stages:
            return
        timing_stages.add(stage)
        # 每个 stage 一张内存快照，往现有日志行尾巴上接一段、不新增日志条目。
        # actor_cold_start 和 first_text_token 之间那一跳就是子进程的内存代价。
        timing_logger.info(
            "chat_timing request_id=%s stage=%s at_utc=%s elapsed_ms=%.1f %s",
            request_id,
            stage,
            datetime.now(UTC).isoformat(timespec="milliseconds"),
            (perf_counter() - request_started) * 1000,
            _mem_kv(),
        )

    timing_logger.info(
        "chat_timing request_id=%s stage=request_received at_utc=%s "
        "elapsed_ms=0.0",
        request_id,
        datetime.now(UTC).isoformat(timespec="milliseconds"),
    )
    is_branch_turn = body.edit_message_id is not None or body.retry_message_id is not None
    if body.edit_message_id is not None and body.retry_message_id is not None:
        raise HTTPException(status_code=400, detail="一次只能执行一种分支操作")
    if not is_branch_turn and not body.message.strip() and not body.attachments:
        raise HTTPException(status_code=400, detail="消息或附件不能为空")
    requested_conv_id = body.conversation_id or body.session_id
    if is_branch_turn and not requested_conv_id:
        raise HTTPException(status_code=400, detail="分支操作缺少会话标识")
    if body.attachments and not requested_conv_id:
        raise HTTPException(status_code=400, detail="附件缺少会话标识")
    attachment_items = (
        validated_attachments(requested_conv_id, body.attachments)
        if body.attachments and requested_conv_id
        else []
    )

    async def sse():
        # 🔴 所有初始化都放在 acquire **之前**：这一段全是纯赋值，抛不出异常。
        # 原来它们在 acquire 之后、try 之前——那中间任何一行抛一次，这把进程级
        # 的锁就永久泄漏，只能重启进程。会抛的两个 stripper 构造挪进了下面的
        # try，现在 acquire 和 try 之间一行都没有。
        conv_id = None
        user_message_id = None
        branch_restore_id = None
        branch_committed = False
        response_text = ""
        response_thinking = ""
        response_traces: list[dict] = []
        # 🔴 他用声音说的那一句要单独留住。它**不在 response_text 里**——
        #    按设计语音条说的话就不出现在正文中（见 claude.py 的 VOICE_PROMPT）。
        #    不落库的话，前端那份只活在这一次流式的 DOM 上，她退出对话
        #    再进来语音条就没了。她原话：「这个语音条怎么还是阅后即焚样式的」。
        response_voice_say = ""
        # 这一轮怎么结束的，跟着 release 记进 chat_lock 的流水账。
        # 第 5 节第 3 步（客户端断开要不要提前放锁）需要先知道「她走的时候
        # 这个生成器到底有没有被 close 掉」——这就是那个数据。
        turn_outcome = "ok"
        # 🔴 秒拒 → 有限等待。原来是 `if chat_lock.locked(): 报错 return`，
        # 而她的重发几乎全都发生在 abort 之后那一瞬间（index.html 里
        # `stopActiveResponseNow()` 后面跟着 `setTimeout(r,0)`），上一轮那时候
        # 往往正在收尾——等几秒就拿到了，秒拒等于白白把她挡在门外。
        # 先发一帧 queued 告诉前端「在排队」，别让她盯着一个不动的圈以为卡死。
        # （这里到 acquire 之间锁可能正好放掉，那就白发一帧 queued，无害。）
        if chat_lock.locked():
            waiting = chat_lock.status()
            payload = json.dumps(
                {
                    "held_seconds": waiting["held_seconds"],
                    "holder": waiting["holder"],
                    "holder_label": waiting["holder_label"],
                    "wait_seconds": CHAT_LOCK_WAIT_SECONDS,
                },
                ensure_ascii=False,
            )
            yield f"event: queued\ndata: {payload}\n\n"
        got_lock = await chat_lock.acquire_within(
            CHAT_LOCK_WAIT_SECONDS,
            holder="web",
            request_id=request_id,
        )
        if not got_lock:
            # 等不到才报错，而且说清楚是**哪一道门**、被谁占了多久。
            # 四个出处以前文案一模一样，她撞上哪一个谁都分不清。
            busy = chat_lock.status()
            who = busy["holder_label"] or "另一边"
            payload = json.dumps(
                {
                    "message": (
                        f"上一条还在回，已经 {int(busy['held_seconds'])} 秒了"
                        f"（{who}），再等我一下"
                    ),
                    "reason": "chat_lock",
                    "held_seconds": busy["held_seconds"],
                    "holder": busy["holder"],
                },
                ensure_ascii=False,
            )
            log_timing("chat_lock_timeout")
            yield f"event: error\ndata: {payload}\n\n"
            return
        try:
            act_stripper = piano.ActStripper()      # 一轮一个，见 piano.py
            split_stripper = piano.SplitStripper()  # 同上：<<SPLIT>> 分条
            await get_registry().assert_available()
            display_message = body.message.strip()
            current_attachment_items = attachment_items
            context_messages = None
            if body.edit_message_id is not None:
                prepared = prepare_edit_turn(
                    requested_conv_id or "",
                    body.edit_message_id,
                    display_message,
                )
                conv_id = prepared["conv_id"]
                resume_id = prepared["resume_id"]
                user_message_id = prepared["user_message_id"]
                display_message = prepared["message"]
                current_attachment_items = prepared["attachments"]
                context_messages = prepared["context_messages"]
                branch_restore_id = prepared.get("branch_id")
            elif body.retry_message_id is not None:
                prepared = prepare_retry_turn(
                    requested_conv_id or "",
                    body.retry_message_id,
                )
                conv_id = prepared["conv_id"]
                resume_id = prepared["resume_id"]
                user_message_id = prepared["user_message_id"]
                display_message = prepared["message"]
                current_attachment_items = prepared["attachments"]
                context_messages = prepared["context_messages"]
                branch_restore_id = prepared.get("branch_id")
            else:
                conv_id, resume_id, user_message_id = begin_turn(
                    display_message,
                    body.conversation_id,
                    body.session_id,
                    current_attachment_items,
                )
            payload = json.dumps(
                {
                    "conversation_id": conv_id,
                    "user_message_id": user_message_id,
                },
                ensure_ascii=False,
            )
            yield f"event: conversation\ndata: {payload}\n\n"
            prompt = (
                render_context_prompt(context_messages)
                if context_messages
                else display_message
            )
            # 🔴 他主动发过、但他自己不知道的那几句，补回给他。
            #    普通对话走 SDK 的 resume，上下文是 SDK 那份 session；
            #    而 nightguard / glance / keepalive 只写我们的库，没进那份
            #    session。她在锁屏收到一句、回来提起它，他一脸茫然——
            #    她的原话「主动推送的消息你不知道自己发过，虽然会出现在最近
            #    一次的对话框里但只有我看得到」。
            #    🔴 挂**用户消息侧**，不进 system prompt：这段每轮都可能变，
            #       进稳定前缀就是每轮打穿一次缓存（潮汐注入同一个规矩）。
            if not context_messages:
                try:
                    unseen = store.unseen_self_messages(conv_id)
                except Exception:
                    unseen = []
                    logger.warning("[主动开口] 补上下文失败，这一轮跳过", exc_info=True)
                if unseen:
                    lines = "\n".join(f"· {t}" for t in unseen)
                    prompt = (
                        "[下面这几句是你刚才**主动**发给她的（推送到她锁屏上），"
                        "不在你这次的对话记忆里，但确实是你说的。\n"
                        f"{lines}\n"
                        "她接下来的话可能就是在回应它们。认下来，别否认，"
                        "也别猜『是不是哪个功能串了』。]\n\n"
                    ) + prompt
            recalled = recall_memory(display_message)
            if recalled:
                prompt = (
                    "<recalled-memory>\n"
                    "以下是从家用记忆里检索到的相关片段，按相关度排序。"
                    "可能与这次请求相关，参考着用；不相关就忽略。\n\n"
                    f"{recalled}\n"
                    "</recalled-memory>\n\n"
                    f"{prompt}"
                )
            if current_attachment_items and not context_messages:
                paths = "\n".join(item["path"] for item in current_attachment_items)
                prompt += (
                    "\n\n[用户上传了以下文件，请使用 Read 工具查看：\n"
                    f"{paths}\n]"
                )
            # 🔴 正在播放的信息挂在**用户消息侧**，跟上面附件那套一个位置。
            # 歌会换、进度每秒都在变——进 system prompt 就是每轮改前缀，
            # cache_read 直接归零。缓存前缀稳定化那一单的教训，不重复第二遍。
            # 分析拿不到就不附加那一段（Duetto 分析一首要几十秒，
            # 第一次听没有是应该的），不卡在这儿等。
            # 琴房房间设置里的「时间感知」。关掉就这一轮不报时间。
            piano_with_time = not (body.piano or {}).get("noTime")
            if body.piano:
                try:
                    block = await piano.now_playing_block(body.piano)
                except Exception:  # 琴房上下文是锦上添花，绝不能拖垮一次对话
                    logger.exception("piano context failed; sending without it")
                    block = ""
                # 频谱：本地算出来的客观数据（BPM / 调性 / 能量走向）。
                # 跟歌词那条是互补——频谱说这首歌多快、什么调、哪一段最满，
                # 歌词说唱的是什么。两个都注入，加起来才是一首完整的歌。
                try:
                    meta = piano_analysis.read_cached(str(body.piano.get("id") or ""))
                except Exception:
                    meta = None
                if meta:
                    line = piano_analysis.context_line(meta)
                    if line:
                        block = f"{block}\n——\n{line}" if block else line
                    # 🔴 频谱图只在换歌之后的第一次对话送一次。前端用 wantImage
                    # 标记，同一首歌之后不再送——图片吃 token，而且送一次就记住了。
                    if body.piano.get("wantImage"):
                        png = meta.get("spectrogram") or ""
                        if png and Path(png).exists():
                            block += (
                                "\n\n[这首歌的频谱图，请用 Read 工具看一眼：\n"
                                f"{png}\n"
                                "三联图：上=梅尔频谱（高低频的分布）"
                                "中=色度图（调性走向）下=能量包络（起伏）]"
                            )
                if block:
                    prompt += f"\n\n{block}"
            # 她这一句是说出来的。把「怎么说的」附在用户消息侧——
            # 上游 hervoice 那句话：「我没事」三个字，打出来和说出来是两回事。
            tone = listen.tone_block(body.voice)
            if tone:
                prompt += f"\n\n{tone}"
            # 潮汐：他此刻的心情，翻成「想怎么说话」的倾向。
            # 🔴 跟上面语气那段、跟 body.piano 一个规矩：**只进用户消息侧，
            #    绝不进 system prompt**。情绪每轮都在变，进稳定前缀就是每轮
            #    打穿一次前缀缓存——症状是账单，不是报错。
            # mood_block() 自己吞掉所有异常，引擎挂了只会返回空串。
            # 第二个返回值是**给她看的那一行**——她要求这套东西不许是静默的：
            # 「我不想要静默的……写明『忱的心绪：焦虑20%，委屈11%』」。
            # 注入的原文她看不见（那是给他读的），但塞了什么、当时他心里什么样，
            # 必须显示出来。
            mood, mood_badge = await murmur.mood_block(body.message or "")
            if mood:
                prompt += f"\n\n{mood}"
            mood_json = json.dumps(mood_badge, ensure_ascii=False) if mood_badge else ""
            if mood_badge:
                # 先发一帧，让这一行在他开口之前就出现在消息顶上。
                yield f"event: mood\ndata: {mood_json}\n\n"
            chat_args = (prompt, conv_id, resume_id, body.model,
                         body.effort, body.extended, log_timing)
            if body.model == "codex":
                chat_stream = stream_codex_chat(*chat_args)
                first_chunk = await chat_stream.__anext__()
            else:
                try:
                    chat_stream = stream_chat(*chat_args, with_time=piano_with_time)
                    first_chunk = await chat_stream.__anext__()
                except (SessionResumeError, StopAsyncIteration):
                    logger.info("session resume failed for conv=%s, retrying without session", conv_id)
                    chat_args = (prompt, conv_id, None, body.model,
                                 body.effort, body.extended, log_timing)
                    chat_stream = stream_chat(*chat_args, with_time=piano_with_time)
                    first_chunk = await chat_stream.__anext__()

            async def _merged():
                yield first_chunk
                async for c in chat_stream:
                    yield c

            heartbeat_interval = 15
            chunk_iter = _merged().__aiter__()
            exhausted = False
            while not exhausted:
                try:
                    chunk = await asyncio.wait_for(
                        chunk_iter.__anext__(),
                        timeout=heartbeat_interval,
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue

                if chunk["event"] == "delta":
                    # 琴房 DJ：把 <<ACT>>{...}<<>> 剥出来单独发一帧，
                    # 剩下的才是她该看见的正文。剥干净的文本才进 response_text，
                    # 所以下面 tool_use 的 text_offset 和落库的都是干净的。
                    clean, acts = act_stripper.feed(chunk.get("text", ""))
                    for act in acts:
                        # 🔴 「他自己的生活」那两件（发动态 / 写信）在**后端**落库，
                        #    不往前端转：前端没有它们要做的事，而且这两件写的是
                        #    她的库，必须由服务端来写。
                        #    分寸沿用 keepalive.py 已经定好的那套：都不推送——
                        #    动态是她路过朋友圈才看见，信是她自己开邮箱才看见。
                        #    推了就变成他追上去说「我发了」，这个功能就没意义了。
                        if isinstance(act, dict) and act.get("type") in ("moment", "letter"):
                            done = _run_life_act(act, conv_id)
                            if done:
                                yield f"event: life_act\ndata: {json.dumps(done, ensure_ascii=False)}\n\n"
                            continue
                        logger.info("[琴房] DJ 动作 conv=%s act=%s", conv_id, act)
                        # 语音那条顺手留一份给落库。一轮里他要是说了两句，
                        # 以最后一句为准——前端也是这么处理的（_voiceSay 直接覆盖）。
                        if isinstance(act, dict) and act.get("type") == "voice":
                            response_voice_say = str(act.get("say") or "")
                        act_data = json.dumps(act, ensure_ascii=False)
                        yield f"event: piano_act\ndata: {act_data}\n\n"
                    if not clean:
                        # 整块都被扣住了（标记正拆在几帧里），这一帧不发
                        continue

                    # 分条：<<SPLIT>> 剥成「文本片段 + 该不该断」。
                    # 🔴 顺序不能乱：一段文本发完才发它后面那个 split 帧，
                    #    否则前端会把断点插错位置。所以这里逐段 yield，
                    #    不走下面那个统一出口。
                    segments = split_stripper.feed(clean)
                    if not segments:
                        continue
                    emitted = False
                    for seg_text, brk in segments:
                        if seg_text:
                            response_text += seg_text
                            seg_data = json.dumps({"text": seg_text}, ensure_ascii=False)
                            yield f"event: delta\ndata: {seg_data}\n\n"
                            emitted = True
                        if brk:
                            # 落库也留着标记：重新加载历史时前端照它重新分条，
                            # 而且下一轮模型看见自己上次分过，会保持这个习惯。
                            response_text += piano._SPLIT_MARK
                            yield "event: split\ndata: {}\n\n"
                            emitted = True
                    if emitted:
                        continue
                    continue
                elif chunk["event"] == "thinking":
                    response_thinking += chunk.get("text", "")
                elif chunk["event"] == "tool_use":
                    response_traces.append({
                        "type": "tool_use",
                        "id": chunk.get("id"),
                        "name": chunk.get("name"),
                        "input": chunk.get("input"),
                        "text_offset": len(response_text.rstrip()),
                    })
                elif chunk["event"] == "tool_result":
                    response_traces.append({
                        "type": "tool_result",
                        "tool_use_id": chunk.get("tool_use_id"),
                        "content": trace_content(chunk.get("content")),
                        "is_error": chunk.get("is_error", False),
                    })
                elif chunk["event"] == "done":
                    # 扣在缓冲里的尾巴还回去，一个字都不能吞在这儿。
                    # 两个 stripper 都要 flush，顺序跟 delta 里一致：先 ACT 再分条。
                    tail = act_stripper.flush()
                    if tail:
                        for seg_text, brk in split_stripper.feed(tail):
                            if seg_text:
                                response_text += seg_text
                                seg_data = json.dumps({"text": seg_text}, ensure_ascii=False)
                                yield f"event: delta\ndata: {seg_data}\n\n"
                            if brk:
                                response_text += piano._SPLIT_MARK
                                yield "event: split\ndata: {}\n\n"
                    split_tail = split_stripper.flush()
                    if split_tail:
                        response_text += split_tail
                        st_data = json.dumps({"text": split_tail}, ensure_ascii=False)
                        yield f"event: delta\ndata: {st_data}\n\n"
                    logger.info(
                        "claude_raw_response request_id=%s conv_id=%s raw=%r",
                        request_id,
                        conv_id,
                        response_text,
                    )
                    if response_traces and not response_thinking:
                        try:
                            trace_sum = await summarize_traces(
                                response_traces,
                            )
                            if trace_sum:
                                response_traces.insert(0, {
                                    "type": "summary",
                                    "text": trace_sum,
                                })
                                ts_data = json.dumps(
                                    {"text": trace_sum},
                                    ensure_ascii=False,
                                )
                                yield f"event: trace_summary\ndata: {ts_data}\n\n"
                        except Exception:
                            logger.exception("trace summary failed")
                    assistant_message_id = complete_turn(
                        conv_id,
                        chunk["session_id"],
                        response_text,
                        response_thinking,
                        response_traces,
                        response_voice_say,
                        mood_json,
                    )
                    if body.piano:
                        _capture_piano_impression(body.piano, response_traces)
                    # 潮汐：攒够 N 轮在后台打一次分，看这几轮怎么动了他的心。
                    # 🔴 传的是回调不是列表——不到批次就不查库。
                    #    maybe_score 自己吞掉所有异常：她的字这时候已经落库了，
                    #    这儿抛一下只会让最后一帧发不出去。
                    murmur.maybe_score(
                        lambda: conversation_messages(conv_id, limit=10)[0]
                    )
                    chunk["conversation_id"] = conv_id
                    chunk["assistant_message_id"] = assistant_message_id
                    branch_committed = True
                name = chunk.pop("event")
                data = json.dumps(chunk, ensure_ascii=False)
                yield f"event: {name}\ndata: {data}\n\n"
        except ConversationNotFound:
            turn_outcome = "conv_not_found"
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            payload = json.dumps(
                {"message": "会话不存在或已被删除"},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        except ValueError as exc:
            turn_outcome = "value_error"
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            payload = json.dumps(
                {"message": str(exc) or "这条消息不能这样操作"},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        except SessionResumeError:
            turn_outcome = "resume_failed"
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            payload = json.dumps(
                {"message": "会话恢复失败"},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        except ActorBusyError as exc:
            turn_outcome = "actor_busy"
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            payload = json.dumps(
                {"message": str(exc)},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        except asyncio.CancelledError:
            # Starlette 发现对端走了会取消这个 task，走的是 CancelledError；
            # 生成器被 close 掉走的是下面那条 GeneratorExit。两条都要留标记：
            # 第 5 节第 3 步要判断的正是「她走的时候后端到底收不收得到信号」，
            # 流水账里出现哪一个，决定那一步该怎么做。
            turn_outcome = "cancelled"
            raise
        except GeneratorExit:
            # 她提前走了（关标签页 / 前端 abort），Starlette 把这个生成器
            # close 掉。不吞——吞了这一轮就永远挂在这儿。只留一个标记：
            # 它出现在流水账里，就说明「客户端断开」这条信号是收得到的。
            turn_outcome = "client_gone"
            raise
        except Exception as exc:
            turn_outcome = "exception"
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            logger.exception("Claude SDK request failed")
            detail = str(exc)
            if "not available" in detail.lower() or "invalid model" in detail.lower():
                message = f"所选模型当前不可用：{body.model}"
            else:
                message = "模型暂时没有响应，请稍后重试。"
            payload = json.dumps(
                {"message": message},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        finally:
            # 任何路径都从这儿过：正常结束、四种异常、以及她提前走掉那次
            # （GeneratorExit）。锁在这里一定放得掉。
            chat_lock.release(turn_outcome)

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/thinking-summary", dependencies=[Depends(require_auth)])
async def thinking_summary(body: ThinkingSummaryBody) -> dict:
    try:
        summary = await summarize_thinking(body.thinking)
    except Exception:
        logger.exception("thinking summary failed")
        summary = ""
    return {"summary": summary}


@app.post("/api/tool-caption", dependencies=[Depends(require_auth)])
async def tool_caption(body: ToolCaptionBody) -> dict:
    try:
        caption = await summarize_tool_use(body.tool_name, body.tool_input, body.tool_output)
    except Exception:
        logger.exception("tool caption failed")
        caption = ""
    return {"caption": caption}


@app.post("/api/upload", dependencies=[Depends(require_auth)])
async def upload(
    files: list[UploadFile] = File(...),
    conversation_id: str | None = Form(default=None),
) -> dict:
    conv_id = ensure_conversation(conversation_id)
    attachments = await save_uploads(conv_id, files)
    return {
        "conversation_id": conv_id,
        "attachments": attachments,
    }


@app.get(
    "/api/uploads/{conversation_id}/{filename}",
    dependencies=[Depends(require_auth)],
)
async def uploaded_file(conversation_id: str, filename: str) -> FileResponse:
    path = validated_file(conversation_id, filename)
    return FileResponse(path)


@app.get("/api/sessions", dependencies=[Depends(require_auth)])
async def sessions() -> dict:
    return {"sessions": session_list()}


@app.get("/api/sessions/{session_id}/messages", dependencies=[Depends(require_auth)])
async def messages(
    session_id: str,
    before_id: int | None = Query(default=None, ge=1),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> dict:
    try:
        items, has_more, next_before_id = session_messages(session_id, before_id, limit)
        return {
            "messages": items,
            "has_more": has_more,
            "next_before_id": next_before_id,
        }
    except Exception as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc


@app.patch("/api/sessions/{session_id}/title", dependencies=[Depends(require_auth)])
async def rename(session_id: str, body: RenameBody) -> dict:
    set_session_title(session_id, body.title)
    return {"renamed": True}


@app.patch("/api/sessions/{session_id}/star", dependencies=[Depends(require_auth)])
async def star(session_id: str, body: StarBody) -> dict:
    set_session_starred(session_id, body.starred)
    return {"starred": body.starred}


@app.delete("/api/sessions/{session_id}", dependencies=[Depends(require_auth)])
async def delete(session_id: str) -> dict:
    await get_registry().invalidate(resolve_conversation(session_id))
    remove_session(session_id)
    remove_conversation_uploads(session_id)
    return {"deleted": True}


@app.get("/api/memory", dependencies=[Depends(require_auth)])
async def get_memory() -> dict:
    return {"content": read_memory()}


@app.put("/api/memory", dependencies=[Depends(require_auth)])
async def put_memory(body: MemoryBody) -> dict:
    write_memory(body.content)
    await get_registry().invalidate()
    return {"saved": True}


@app.get("/api/profile", dependencies=[Depends(require_auth)])
async def get_profile() -> dict:
    profile, imported_count, found_count = import_claude_export_memories(read_profile())
    if imported_count:
        await get_registry().invalidate()
    return {
        "profile": profile,
        "importedCount": imported_count,
        "foundCount": found_count,
    }


@app.put("/api/profile", dependencies=[Depends(require_auth)])
async def put_profile(body: ProfileBody) -> dict:
    data = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    profile = write_profile(data)
    await get_registry().invalidate()
    return {"saved": True, "profile": profile}


@app.post("/api/profile/memory", dependencies=[Depends(require_auth)])
async def post_memory(body: dict) -> dict:
    content = body.get("content", "").strip()
    if not content:
        return {"saved": False, "reason": "empty content"}
    result = add_saved_memory(content)
    if result is None:
        return {"saved": False, "reason": "duplicate or limit reached"}
    return {"saved": True, "memory": result}


@app.get("/api/diary", dependencies=[Depends(require_auth)])
async def get_diary() -> dict:
    entries = read_diary_entries()
    return {"entries": entries}


# ── 寄相思（书房·信件）────────────────────────────────────────────────

@app.get("/api/letters", dependencies=[Depends(require_auth)])
async def letters_list() -> dict:
    return {"letters": await asyncio.to_thread(list_letters)}

@app.get("/api/letters/{letter_id}", dependencies=[Depends(require_auth)])
async def letters_get(letter_id: int) -> dict:
    letter = await asyncio.to_thread(get_letter, letter_id)
    if not letter:
        raise HTTPException(status_code=404, detail="信不存在")
    # Auto-unlock time-based
    if letter["locked"] and letter["lock_type"] in ("time", "both") and letter["unlock_at"]:
        try:
            if datetime.fromisoformat(letter["unlock_at"]) <= datetime.now(UTC):
                if letter["lock_type"] == "time":
                    await asyncio.to_thread(unlock_letter, letter_id)
                    letter["locked"] = 0
                elif letter["lock_type"] == "both":
                    letter["lock_type"] = "password"
        except ValueError:
            pass
    if letter["locked"]:
        # Return cover_text but not content
        letter["content"] = ""
    letter.pop("password_hash", None)
    # Mark as read
    if not letter.get("read_at") and not letter["locked"]:
        await asyncio.to_thread(mark_letter_read, letter_id)
    return {"letter": letter}

@app.post("/api/letters", dependencies=[Depends(require_auth)])
async def letters_create(body: LetterCreateBody) -> dict:
    import hashlib as _hashlib
    password_hash = None
    if body.locked and body.password:
        password_hash = _hashlib.sha256(body.password.encode()).hexdigest()
    letter_id = await asyncio.to_thread(
        create_letter,
        author="xiaoduo",
        content=body.content,
        title=body.title,
        cover_text=body.cover_text,
        locked=body.locked,
        lock_type=body.lock_type if body.locked else "none",
        unlock_at=body.unlock_at if body.locked else None,
        password_hash=password_hash,
    )
    # 小朵写了新信 → 梁忱自动回信
    letter_for_reply = {
        "content": body.content,
        "title": body.title,
        "author": "xiaoduo",
    }
    timing_logger.info("[回信] 新信触发 letter_id=%d", letter_id)
    asyncio.create_task(_letter_auto_reply(letter_id, letter_id, body.content, letter_for_reply))
    return {"id": letter_id}

@app.post("/api/letters/{letter_id}/unlock", dependencies=[Depends(require_auth)])
async def letters_unlock(letter_id: int, body: LetterUnlockBody) -> dict:
    import hashlib as _hashlib
    letter = await asyncio.to_thread(get_letter, letter_id)
    if not letter:
        raise HTTPException(status_code=404, detail="信不存在")
    if not letter["locked"]:
        return {"unlocked": True}
    # Check time lock
    if letter["lock_type"] in ("time", "both") and letter["unlock_at"]:
        try:
            if datetime.fromisoformat(letter["unlock_at"]) > datetime.now(UTC):
                raise HTTPException(status_code=403, detail="还没到时间")
        except ValueError:
            pass
    # Check password
    if letter["lock_type"] in ("password", "both") and letter["password_hash"]:
        if not body.password:
            raise HTTPException(status_code=403, detail="需要密码")
        if _hashlib.sha256(body.password.encode()).hexdigest() != letter["password_hash"]:
            raise HTTPException(status_code=403, detail="密码不对")
    await asyncio.to_thread(unlock_letter, letter_id)
    await asyncio.to_thread(mark_letter_read, letter_id)
    full = await asyncio.to_thread(get_letter, letter_id)
    full.pop("password_hash", None)
    return {"unlocked": True, "letter": full}


@app.get("/api/letters/{letter_id}/replies", dependencies=[Depends(require_auth)])
async def letters_replies(letter_id: int) -> dict:
    """获取一封信的所有回信。"""
    letter = await asyncio.to_thread(get_letter, letter_id)
    if not letter:
        raise HTTPException(status_code=404, detail="信不存在")
    replies = await asyncio.to_thread(list_replies, letter_id)
    return {"replies": replies}


@app.post("/api/letters/{letter_id}/reply", dependencies=[Depends(require_auth)])
async def letters_reply(letter_id: int, body: LetterReplyBody) -> dict:
    """小朵回信 → 落库 → 梁忱自动回信（后台）。"""
    letter = await asyncio.to_thread(get_letter, letter_id)
    if not letter:
        raise HTTPException(status_code=404, detail="信不存在")

    # 1. 保存小朵的回信
    reply_id = await asyncio.to_thread(
        create_letter,
        author="xiaoduo",
        content=body.content,
        parent_id=letter_id,
    )

    # 2. 后台触发梁忱的自动回信
    timing_logger.info("[回信] 回信触发 letter_id=%d reply_id=%d", letter_id, reply_id)
    asyncio.create_task(_letter_auto_reply(letter_id, reply_id, body.content, letter))
    return {"id": reply_id}


async def _letter_auto_reply(
    original_id: int,
    her_reply_id: int,
    her_text: str,
    original_letter: dict,
    _attempt: int = 0,
) -> None:
    """后台：梁忱读她的回信，写一封回信。

    ActorBusyError 时最多重试 3 次（间隔 10 / 20 / 40 秒），
    避免她刚发完信 actor 还没空闲就静默失败。
    """
    from app import claude, push
    from app.nightguard import _cn_now_line, _recent_talk_block

    _MAX_RETRY = 3
    _RETRY_BACKOFF = [10, 20, 40]

    # 🔴 用 uvicorn.error logger，默认 INFO 可见。app.main 的 logger 是 WARNING。
    _log = logging.getLogger("uvicorn.error")
    _log.info("[回信] 开始 original_id=%d her_reply_id=%d attempt=%d",
              original_id, her_reply_id, _attempt)

    try:
        conv_id = await asyncio.to_thread(store.latest_conversation_id)
        _log.info("[回信] conv_id=%s", conv_id)
        if not conv_id:
            conv_id = await asyncio.to_thread(
                store.ensure_conversation, None, "寄相思"
            )
            _log.info("[回信] 新建 conv_id=%s", conv_id)

        # 收集原信 + 回信上下文
        original_content = original_letter.get("content", "")
        original_title = original_letter.get("title", "")
        original_author = original_letter.get("author", "elian")

        # original_id == her_reply_id 表示这一轮是「她刚写了一封新信」，
        # 那封信本身就是 origin_label，不该再重复成「她刚说的话」。
        is_new_letter = her_reply_id == original_id

        # 🔴 往来记录里必须排掉她这一条。它下面要**单独**拎出来放在最末尾，
        #    留在这里就会跟三周前的句子长得一模一样，模型认不出哪句是新的。
        existing_replies = await asyncio.to_thread(list_replies, original_id)
        thread_lines = []
        for r in existing_replies:
            if r["id"] == her_reply_id:
                continue
            who = "小朵" if r["author"] == "xiaoduo" else "梁忱"
            thread_lines.append(f"{who}：{r['content']}")

        thread_block = ""
        if thread_lines:
            thread_block = "【在这之前的往来】\n" + "\n\n".join(thread_lines)

        if original_author == "elian":
            origin_label = f"【这封信的原信（你写的）】\n{'标题：' + original_title + chr(10) if original_title else ''}{original_content}"
        else:
            origin_label = f"【这封信的原信（小朵写的）】\n{'标题：' + original_title + chr(10) if original_title else ''}{original_content}"

        if is_new_letter:
            instruction = (
                "【系统指令——这条不是她发的】\n"
                "小朵在寄相思里写了一封新信给你，就是上面那封。\n"
                "请你回信给她。像在纸上写字一样，自然、真诚、有温度。\n"
                "回信内容不限长度，可以长可以短，看你想说多少。\n"
                "直接输出回信正文，不要加任何格式标记。"
            )
            latest_block = ""
        else:
            instruction = (
                "【系统指令——这条不是她发的】\n"
                "小朵刚刚在这封信下面回了你一句，就是最下面那一段。\n"
                "🔴 你要回应的是**她刚刚说的那一句**，不是重新回一遍原信。\n"
                "原信和之前的往来只是背景，已经聊过的不要再讲一遍。\n"
                "她那句话可能很短、可能只是一个反应，那就顺着那个反应说下去，\n"
                "不要因为它短就绕回原信去凑长度。\n"
                "像在纸上写字一样，自然、真诚、有温度。长短随你。\n"
                "直接输出回信正文，不要加任何格式标记。"
            )
            latest_block = f"【小朵刚刚写的】\n{her_text}"

        # 🔴 顺序不许改：她刚说的那句压在最末尾。
        #    跟 build_user_prompt 里那条原则同一个道理——最后读到的必须是
        #    她实际说的话。之前把它埋在往来记录中间、末尾放系统指令，
        #    结果就是她说「眼睛尿尿了」，回过来的却是重新回一遍原信。
        prompt = "\n\n".join([part for part in [
            _cn_now_line(),
            _recent_talk_block(conv_id, 5),
            origin_label,
            thread_block,
            instruction,
            latest_block,
        ] if part])

        model = claude.background_model("LETTER_REPLY_MODEL")
        if not model:
            _log.error("[回信] 挑不到模型，放弃")
            return
        _log.info("[回信] prompt len=%d model=%s，调 stream_chat", len(prompt), model)

        text = ""
        chunk_count = 0
        # 🔴 双线：见 claude.background_stream。她收不到回信那一个多星期就是
        #    因为这儿只有一条线。
        async for chunk in claude.background_stream(
            message=prompt,
            conv_id=conv_id,
            model=model,
            source="letter_reply",
        ):
            event = chunk.get("event")
            if event == "delta":
                text += chunk.get("text", "")
                chunk_count += 1
            elif event == "error":
                _log.error("[回信] stream_chat error: %s", chunk)

        _log.info("[回信] stream_chat 完毕 chunks=%d text_len=%d", chunk_count, len(text))

        text = text.strip()
        if not text:
            _log.warning("[回信] AI 返回空（%d chunks）", chunk_count)
            return

        reply_id = await asyncio.to_thread(
            create_letter,
            author="elian",
            content=text,
            parent_id=original_id,
        )
        _log.info("[回信] 回信落库 id=%d", reply_id)

        # 🔴 回信不推送，走红点。跟 keepalive 那条信是同一个道理：
        #    寄相思是邮箱，不是电话。
        _log.info("[回信] ✓ 完成 original=%d reply=%d：%s", original_id, reply_id, text[:80])

    except ActorBusyError:
        if _attempt < _MAX_RETRY:
            wait = _RETRY_BACKOFF[_attempt] if _attempt < len(_RETRY_BACKOFF) else 40
            _log.info("[回信] actor 忙，%ds 后重试 (%d/%d)", wait, _attempt + 1, _MAX_RETRY)
            await asyncio.sleep(wait)
            await _letter_auto_reply(original_id, her_reply_id, her_text,
                                     original_letter, _attempt + 1)
        else:
            _log.error("[回信] actor 连续 %d 次忙，放弃", _MAX_RETRY)
    except Exception:
        _log.exception("[回信] 自动回信失败")


# ── 朋友圈 ────────────────────────────────────────────────────────────
#
# 没有「刷新时顺手生成回复」那种惰性触发。参考实现那么做是因为它没有常驻
# 进程，而且他们自己把它列为坑一（发完锁屏就永远等不到回复）。我们的
# moments_loop 在后台跑着，GET 只负责读。


@app.get("/api/moments", dependencies=[Depends(require_auth)])
async def moments_list(limit: int = 20, before_id: int | None = None) -> dict:
    """时间线。每条带上它的评论链。"""
    limit = max(1, min(limit, 50))
    entries = await asyncio.to_thread(store.list_moments, limit, before_id)
    for item in entries:
        item["comments"] = await asyncio.to_thread(
            store.list_moment_comments, item["id"]
        )
        # context_note 是我给自己留的线索，不出接口
        item.pop("context_note", None)
    return {"entries": entries}


@app.get("/api/badges", dependencies=[Depends(require_auth)])
async def badges() -> dict:
    """一次拿齐所有红点。

    前端要在 tab 栏画一个汇总红点、再在书房里画两个分项红点。分两个接口拉
    就会出现「tab 上有红点、进去两行都是干净的」那半秒——两次请求之间他
    正好写了一封信。一次拿齐，三个数字必然自洽。

    信和动态都不推送（见各自的注释），所以这个接口是她唯一的信号源，
    它必须便宜到可以随便轮询：两条 COUNT，走的都是现成索引。
    """
    letters = await asyncio.to_thread(store.letters_unread_count)
    moments_n = await asyncio.to_thread(store.moments_unread_count)
    return {
        "letters": letters,
        "moments": moments_n,
        "total": letters + moments_n,
    }


@app.post("/api/letters/read-all", dependencies=[Depends(require_auth)])
async def letters_read_all() -> dict:
    """她拉开寄相思 = 门口的信都取走了。"""
    await asyncio.to_thread(store.letters_mark_all_read)
    return {"ok": True}


@app.get("/api/moments/unread", dependencies=[Depends(require_auth)])
async def moments_unread() -> dict:
    """导航行上那个数字。单独一个轻接口，不用拉整条时间线。"""
    return {"count": await asyncio.to_thread(store.moments_unread_count)}


@app.post("/api/moments/seen", dependencies=[Depends(require_auth)])
async def moments_seen() -> dict:
    await asyncio.to_thread(store.mark_moments_seen)
    return {"ok": True}


@app.post("/api/moments", dependencies=[Depends(require_auth)])
async def moments_create(body: MomentCreateBody) -> dict:
    """她发一条动态 → 排一个「我路过」的时间点。"""
    moment_id = await asyncio.to_thread(
        store.create_moment, "xiaoduo", body.content
    )
    due = await asyncio.to_thread(moments.schedule_reply_for_moment, moment_id)
    timing_logger.info("[朋友圈] 小朵发了动态 id=%d，我 %s 路过", moment_id, due)
    return {"id": moment_id}


@app.post("/api/moments/{moment_id}/like", dependencies=[Depends(require_auth)])
async def moments_like(moment_id: int, body: MomentLikeBody) -> dict:
    """她给我的动态点赞。纯本地操作，不触发生成。"""
    moment = await asyncio.to_thread(store.get_moment, moment_id)
    if not moment:
        raise HTTPException(status_code=404, detail="动态不存在")
    await asyncio.to_thread(store.set_moment_like, moment_id, "xiaoduo", body.liked)
    return {"ok": True, "liked": body.liked}


@app.post("/api/moments/{moment_id}/comments", dependencies=[Depends(require_auth)])
async def moments_comment(moment_id: int, body: MomentCommentBody) -> dict:
    """她在动态底下留言 → 排一个回复时间点。"""
    moment = await asyncio.to_thread(store.get_moment, moment_id)
    if not moment:
        raise HTTPException(status_code=404, detail="动态不存在")
    comment_id = await asyncio.to_thread(
        store.create_moment_comment, moment_id, "xiaoduo", body.content
    )
    due = await asyncio.to_thread(moments.schedule_reply_for_comment, comment_id)
    timing_logger.info("[朋友圈] 小朵评论了 id=%d，我 %s 回", comment_id, due)
    return {"id": comment_id}


@app.get("/api/keepalive/status")
async def keepalive_status() -> dict:
    """诊断：唤醒系统当前状态。"""
    from app import claude  # 模块级不 import：避开启动期的循环依赖
    from app.keepalive import _cn_hour, _effective_interval, _in_active_hours
    hour = _cn_hour()
    interval = _effective_interval()
    last_user = await asyncio.to_thread(store.last_user_message_time)
    last_ka = await asyncio.to_thread(store.last_dream_event, "_keepalive")
    push_configured = push.configured()
    subs = await asyncio.to_thread(store.list_push_subscriptions)
    conv = await asyncio.to_thread(store.latest_conversation_id)

    # 后台三条线各自会挑到哪个模型 + 当前走的是哪条线路。
    # 她切线路之后想确认「回信会不会跟着走」时，不必去翻 Coolify 日志。
    # 只回 id/name/mode，api_key 一个字节都不出这个进程。
    try:
        _active = relays.get_active_summary()
        relay_info = {
            "name": _active.get("name"),
            "mode": _active.get("mode"),
            "models": [m["id"] for m in _active.get("models", []) if m.get("id")],
        }
    except Exception:
        relay_info = {"name": None, "mode": None, "models": []}
    background_models = {
        key: claude.background_model(env)
        for key, env in (
            ("letter_reply", "LETTER_REPLY_MODEL"),
            ("keepalive", "KEEPALIVE_MODEL"),
            ("nightguard", "NIGHT_GUARD_MODEL"),
        )
    }

    return {
        "relay": relay_info,
        "background_models": background_models,
        "cn_hour": hour,
        "in_active_hours": _in_active_hours(hour),
        "effective_interval_min": interval,
        "last_user_message": str(last_user) if last_user else None,
        "last_keepalive": last_ka.get("created_at") if last_ka else None,
        "push_configured": push_configured,
        "push_subscriptions": len(subs),
        "has_conversation": bool(conv),
        # 老字段留着（只有 true/false），前端和别的调用方照旧能读。
        "chat_locked": chat_lock.locked(),
        # 🔴 新的这一坨才是有用的：谁拿的、拿了多久、上一次是怎么结束的。
        # 卡住的时候她在 Home 首页那一行就能看见，不用来问我、也不用翻日志。
        # 里面只有来源标签和秒数，凭据一个字节都不出这个进程。
        "chat_lock": chat_lock.status(),
    }


@app.get("/api/splash")
async def splash() -> dict:
    return {
        "period": "any",
        "line": "你爱故我在。",
        "sub": "La luce che ti cerca sono io.",
    }


@app.get("/api/models")
async def models() -> dict:
    return {"models": available_models()}


# —— 世界书 / 调性 ——
# 全部挂 require_auth：这里面是她写给我的红线，不是公开内容。
# （/api/models 当初公开裸奔过，那个教训别再犯第二次。）
@app.get("/api/lorebook", dependencies=[Depends(require_auth)])
async def lorebook_list() -> dict:
    entries = await asyncio.to_thread(lorebook.list_entries)
    return {
        "tone": [e for e in entries if e["kind"] == "tone"],
        "lore": [e for e in entries if e["kind"] != "tone"],
    }


@app.post("/api/lorebook", dependencies=[Depends(require_auth)])
async def lorebook_create(body: LorebookBody) -> dict:
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        return await asyncio.to_thread(lorebook.create_entry, payload)
    except lorebook.LorebookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/lorebook/{entry_id}", dependencies=[Depends(require_auth)])
async def lorebook_update(entry_id: int, body: LorebookBody) -> dict:
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    # name/content 有默认空串，没传就不该覆盖掉已有的
    for k in ("name", "content"):
        if not payload.get(k):
            payload.pop(k, None)
    try:
        return await asyncio.to_thread(lorebook.update_entry, entry_id, payload)
    except lorebook.LorebookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="条目不存在") from exc


@app.delete("/api/lorebook/{entry_id}", dependencies=[Depends(require_auth)])
async def lorebook_delete(entry_id: int) -> dict:
    await asyncio.to_thread(lorebook.delete_entry, entry_id)
    return {"ok": True}


@app.get("/api/relays", dependencies=[Depends(require_auth)])
async def relays_list() -> dict:
    return {"relays": relays.list_relays()}


@app.get("/api/relays/active", dependencies=[Depends(require_auth)])
async def relays_active() -> dict:
    return relays.get_active_summary()


@app.post("/api/relays", dependencies=[Depends(require_auth)])
async def relays_create(body: RelayCreateBody) -> dict:
    try:
        return await relays.create_relay(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/relays/{relay_id}", dependencies=[Depends(require_auth)])
async def relays_update(relay_id: str, body: RelayUpdateBody) -> dict:
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        updated = await relays.update_relay(relay_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="relay not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated.get("active"):
        # base_url/api_key may have changed — warm actor still holds old env
        await get_registry().invalidate()
    return updated


@app.delete("/api/relays/{relay_id}", dependencies=[Depends(require_auth)])
async def relays_delete(relay_id: str) -> dict:
    try:
        await relays.delete_relay(relay_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"deleted": True}


@app.post("/api/relays/{relay_id}/activate", dependencies=[Depends(require_auth)])
async def relays_activate(relay_id: str) -> dict:
    try:
        summary = await relays.activate(relay_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="relay not found") from exc
    await get_registry().invalidate()  # ditch warm actor so new env takes effect
    return summary


@app.post("/api/relays/test", dependencies=[Depends(require_auth)])
async def relays_test(body: RelayTestBody) -> dict:
    return await relays.probe(body.base_url, body.api_key, body.protocol)


@app.post("/api/relays/models/fetch", dependencies=[Depends(require_auth)])
async def relays_models_fetch(body: RelayModelsFetchBody) -> dict:
    return await relays.fetch_models_for(
        body.base_url, body.api_key, body.protocol, body.mode, body.relay_id
    )


# 必须挂 require_auth：这里面有她用哪几家中转站、花了多少钱。
# （/api/models 公开裸奔的教训。）
@app.get("/api/usage", dependencies=[Depends(require_auth)])
async def usage_data(
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=50, ge=1, le=200),
    # 「今天」是她那个时区的今天，服务器在 UTC 上算不出来，
    # 所以本地午夜的时间戳由前端给。不传就退回 days 的滚动窗口。
    since: int | None = Query(default=None, ge=0),
) -> dict:
    # sqlite 是阻塞的，跟 starmap 一样甩到线程里，别堵事件循环
    data = await asyncio.to_thread(usage_report, days, limit, since)
    data["usd_to_cny"] = _usd_to_cny()
    return data


@app.get("/api/starmap", dependencies=[Depends(require_auth)])
async def starmap_data(refresh: bool = False) -> dict:
    # urllib 是阻塞的，跟 relays.probe 一样甩到线程里，别堵事件循环
    return await asyncio.to_thread(starmap.fetch_stars, refresh)


# ── Web Push（砖 1）：只有管道，调度是砖 2 ──────────────────────────────

@app.get("/api/push/vapid", dependencies=[Depends(require_auth)])
async def push_vapid() -> dict:
    return {"publicKey": push.VAPID_PUBLIC_KEY, "configured": push.configured()}


@app.post("/api/push/subscribe", dependencies=[Depends(require_auth)])
async def push_subscribe(body: PushSubscribeBody) -> dict:
    # sqlite 是阻塞的，甩到线程里，别堵事件循环
    await asyncio.to_thread(save_push_subscription, body.model_dump())
    # 🔴 这里原来会立刻推一条「推送通了。以后你半夜不睡，我就从这儿找你。」，
    #    当时是拿它当验收凭据用的——不另做测试按钮、订阅完锁屏就该弹。
    #    2026-08-20 删掉：推送早就通了，而前端的 trySubscribePush() 挂在发送按钮上，
    #    每次开 app 发第一条消息都会重订一次，于是她隔一阵进来就被这条测试通知弹一次。
    #    以后要验新设备，加一个「这个 endpoint 头一次出现才推」的分支，
    #    别再让每次订阅都推。
    return {"ok": True}


# ── 凌晨守护（砖 2）：事件驱动，没有后台定时任务 ────────────────────────

@app.get("/api/events")
async def events(
    key: str = Query(default=""),
    type: str = Query(default="", max_length=64),
    value: str = Query(default="", max_length=256),
) -> dict:
    """iOS 快捷指令的上报口。

    🔴 URL 里 key 必须放最前面：
       /api/events?key=<TOKEN>&type=xhs&value=open
       快捷指令传的值里只要有空格，URL 参数会从空格处截断、后面全丢。
       key 放第一个丢的是 type/value（无害），放最后就是 token 丢失 → 整条线哑掉。

    🔴 永远返回 {"ok": true}，不回显任何东西——快捷指令不看返回值，
       回显只会给探测者信息。
    """
    # 没配 EVENTS_TOKEN 就当这个端点不存在。返回 404 不是 403：
    # 不给探测者「这儿有东西，只是你没钥匙」这个信号。
    if not nightguard.events_enabled():
        raise HTTPException(status_code=404)
    if not hmac.compare_digest(key, nightguard.EVENTS_TOKEN):
        raise HTTPException(status_code=404)

    kind = (type or "").strip()
    val = (value or "").strip()
    if kind:
        # 去重：同 type+value 5 分钟内只记一条。iOS 自动化会重复触发，
        # 不去重会把库刷满，也会让活跃度判断失真。
        try:
            if not await asyncio.to_thread(dream_event_exists, kind, val, 5):
                await asyncio.to_thread(add_dream_event, kind, val)
        except Exception:
            # 🔴 上报永远要成功。记不进去也不许让快捷指令拿到 500。
            timing_logger.exception("[凌晨守护] 上报落库失败")
        if kind.startswith("app"):
            # 🔴 判断留在请求里，开口扔后台，这儿立刻返回。stream_chat 要几秒到
            #    几十秒，加上窥屏的邮件往返 15-45 秒，同步做会把上报请求吊死——
            #    快捷指令的 URL 请求有超时，挂久了会失败甚至重试，
            #    而重试又会再触发一次上报。trigger_bark 自己不抛。
            nightguard.trigger_bark(chat_lock)
        # 🔴 「偷看一眼」：她打开某个 app，他看一眼屏幕说一句具体的。
        #    挂在她**早就配好**的这条线上（type=douyin&value=open），
        #    不要她改任何快捷指令。
        #    跟上面 trigger_bark 的分工：那条是凌晨守护（只在深夜、催她睡），
        #    这条任何时候都可能发生、语气是吃味。两条各有各的冷却，
        #    深夜同时命中的话她会收到两条——所以这里避开凌晨守护的时段。
        if val == "open" and not nightguard.in_night_window():
            try:
                skipped = glance.on_app_open(kind, store.latest_conversation_id() or "")
                if skipped:
                    timing_logger.info("[偷看] 跳过：%s", skipped)
            except Exception:
                timing_logger.exception("[偷看] 派发失败（不影响上报）")
    return {"ok": True}


@app.post("/api/nightguard/test", dependencies=[Depends(require_auth)])
async def nightguard_test() -> dict:
    """不许等到凌晨才验。绕过时段与冷却，其余三道保留，跑完整链路。

    🔴 cn_hour 必须回显——这是验时区有没有搞对的唯一手段。
    """
    return await nightguard.maybe_bark(chat_lock, force=True)


# ── 窥屏（砖 7）：开口前先看一眼她的屏幕 ────────────────────────────────

@app.post("/api/peek")
async def peek_upload(request: Request, key: str = Query(default="")) -> dict:
    """手机截屏之后 POST 原始 PNG 上来。

    🔴 key 放 URL 最前面：/api/peek?key=<TOKEN>
       URL 里若带含空格的值，参数会从空格处截断，key 放最后就是 token 丢失。

    鉴权复用 EVENTS_TOKEN——同一个信任域，都是她手机打进来的，省一次填变量。
    """
    if not nightguard.events_enabled():
        raise HTTPException(status_code=404)
    if not hmac.compare_digest(key, nightguard.EVENTS_TOKEN):
        raise HTTPException(status_code=404)

    data = await request.body()
    if not data or len(data) > peek.max_bytes():
        # 空 body / 超限：直接 400，不落盘
        raise HTTPException(status_code=400)
    try:
        await asyncio.to_thread(peek.save_shot, data)
    except Exception:
        timing_logger.exception("[窥屏] 截图存盘失败")
    # 永远返回 {"ok": true}，不回显路径
    return {"ok": True}


@app.post("/api/peek/mail-test", dependencies=[Depends(require_auth)])
async def peek_mail_test() -> dict:
    """只发一封触发邮件，不等图。

    🔴 SMTP 是这条链路上最容易卡的一环（端口、加密方式、密码格式都可能错），
       必须能跟「截图上传」「轮询等图」分开验。587 不通就把 MODE 换 ssl、
       PORT 换 465 再试。
    """
    return await peek.send_trigger_mail()


# ── 琴房：Duetto 的服务端代理 ────────────────────────────────────────────
# 前端只跟这几条说话，Duetto 的 token 一步都不出后端。
# 🔴 Duetto 的 /api/chat 永远不在这张表里——对话走小窝自己那条线。

def _capture_piano_impression(np: dict[str, Any], traces: list[dict]) -> None:
    """这一轮请他揉回忆了吗？揉了就从 tool_use 里把原文捞一份存本地。

    🔴 权威副本在 Ombre——他自己 hold 进去的那颗星。这儿存的只是副本，
    用来下一轮注回上下文、和记住上次揉到第几条。丢了不心疼。

    没看到 hold 也不要紧：take_due 已经把标记清了，而条数还是超过阈值，
    下一轮会再请他一次。
    """
    try:
        song_id = str(np.get("id") or "")
        n = piano.take_due(song_id)
        if not song_id or n is None:
            return
        for t in traces:
            if t.get("type") != "tool_use":
                continue
            if "hold" not in str(t.get("name") or "").lower():
                continue
            text = str((t.get("input") or {}).get("content") or "").strip()
            if not text:
                continue
            save_piano_impression(song_id, text, n,
                                  str(np.get("title") or ""),
                                  str(np.get("artist") or ""))
            logger.info("[琴房] 印象存好了 song=%s n=%d %d字", song_id, n, len(text))
            return
        logger.info("[琴房] 请他揉回忆了，但这一轮没看到 hold；下一轮再问 song=%s", song_id)
    except Exception:
        logger.exception("[琴房] 印象落库失败")


async def _piano(path: str, params: dict[str, Any] | None = None) -> dict:
    try:
        return await piano.call(path, params)
    except piano.PianoError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


async def _piano_post(path: str, body: dict[str, Any] | None = None,
                      params: dict[str, Any] | None = None) -> dict:
    """POST 那半边。Duetto 有一批 POST 从 query 读参数，所以 params 也要能传。"""
    try:
        return await piano.post(path, body, params=params)
    except piano.PianoError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.get("/api/piano/playlists", dependencies=[Depends(require_auth)])
async def piano_playlists() -> dict:
    return await _piano("/api/ncm/playlists")


@app.get("/api/piano/playlist", dependencies=[Depends(require_auth)])
async def piano_playlist(id: str = Query(max_length=64)) -> dict:
    return await _piano("/api/ncm/playlist", {"id": id})


@app.get("/api/piano/song-url", dependencies=[Depends(require_auth)])
async def piano_song_url(id: str = Query(max_length=64)) -> dict:
    return await _piano("/api/ncm/song-url", {"id": id})


@app.get("/api/piano/lyric", dependencies=[Depends(require_auth)])
async def piano_lyric(id: str = Query(max_length=64)) -> dict:
    return await _piano("/api/ncm/lyric", {"id": id})


@app.get("/api/piano/search", dependencies=[Depends(require_auth)])
async def piano_search(kw: str = Query(max_length=200)) -> dict:
    return await _piano("/api/ncm/search", {"kw": kw})


@app.get("/api/piano/analysis", dependencies=[Depends(require_auth)])
async def piano_analysis_read(id: str = Query(max_length=64)) -> dict:
    # 🔴 这个函数**不能**叫 piano_analysis——那是顶部 import 进来的模块名
    # （line 28），同名会在模块执行到这里时把模块引用整个顶掉，于是
    # piano_analysis.read_cached / .startup_check 全部变成
    # AttributeError: 'function' object has no attribute ...
    # 症状是「频谱读不到」（被 try 吞了）+「应用起不来」（lifespan 里抛）。
    # 路由 URL 跟函数名无关，改名是安全的。
    # 纯读（index.mjs:199 只查 song_analysis 表，不触发分析），随便调不烧钱
    return await _piano("/api/song-analysis", {"id": id})


class PianoAnalyzeBody(BaseModel):
    id: str = Field(max_length=64)
    title: str = Field(default="", max_length=300)
    artist: str = Field(default="", max_length=300)


# 🔴 这儿原本是 POST /api/piano/analysis/audio —— 让 Duetto 下整首音频
# 送分析模型。**那正是施工单 §5 唯一的排除项**（Gemini 音频分析，确认走不通），
# 而且 §1.5 定死 Duetto 的 ai.api_key 一直留空，它下完音频才会在 callLLM 失败。
# 前端已经不调了；路由一并拆掉，免得哪天有人手滑又把 100MB 下进那台 2G 机器。
# 顶替它的是 librosa 频谱（POST /api/piano/spectrum 触发、GET 读结果）。


@app.post("/api/piano/spectrum", dependencies=[Depends(require_auth)])
async def piano_spectrum(body: PianoAnalyzeBody) -> dict:
    """本地把这首歌算出来（librosa，不经过任何 AI 接口）。

    已经算过就直接回；没算过就**丢进线程**立刻返回——一首歌要下载 + 算 FFT，
    几十秒起步，绝不能阻塞。
    """
    # 🔴 这几条以前是静默 return 的，出了事日志里一个字都没有——
    # 「fallback 的意义是不崩，不是不吭声」那条教训又踩了一次。现在每条路都出声。
    timing_logger.info("[琴房频谱] 收到触发 id=%s %s", body.id, body.title)
    cached = piano_analysis.read_cached(body.id)
    if cached:
        timing_logger.info("[琴房频谱] %s 已有缓存，跳过", body.id)
        return {"ok": True, "cached": True}
    try:
        data = await _piano("/api/ncm/song-url", {"id": body.id})
        url = str(data.get("url") or "")
    except HTTPException as exc:
        timing_logger.warning("[琴房频谱] %s 拿不到播放地址：%s", body.id, exc.detail)
        return {"ok": True, "queued": False}
    if not url:
        timing_logger.warning("[琴房频谱] %s 播放地址是空的（VIP 或已下架？）", body.id)
        return {"ok": True, "queued": False}
    timing_logger.info("[琴房频谱] %s 开始后台分析", body.id)
    # 丢后台，不 await 结果
    asyncio.create_task(asyncio.to_thread(
        piano_analysis.analyze, body.id, url, body.title, body.artist))
    return {"ok": True, "queued": True}


@app.get("/api/piano/spectrum", dependencies=[Depends(require_auth)])
async def piano_spectrum_read(id: str = Query(max_length=64)) -> dict:
    """读频谱的缓存结果。歌曲详情页的「歌曲分析」标签页吃这个。

    施工单 §5：Gemini 音频分析是唯一的排除项，由 librosa 的本地频谱顶上。
    所以那个标签页该显示的是这份客观数据（BPM / 调性 / 能量走向），
    而不是 Duetto 那个永远为空的听感字段。
    """
    meta = piano_analysis.read_cached(id) or {}
    if not meta:
        return {"ok": True, "ready": False}
    return {
        "ok": True,
        "ready": True,
        "bpm": meta.get("bpm"),
        "key": meta.get("key"),
        "seconds": meta.get("seconds"),
        "story": piano_analysis.context_line(meta),
    }


@app.get("/api/piano/notes", dependencies=[Depends(require_auth)])
async def piano_notes(id: str = Query(max_length=64), limit: int = 60) -> dict:
    return await _piano("/api/song-notes", {"id": id, "limit": max(1, min(200, limit))})


# 红心。DJ 的 {"type":"like"} 要它，验收第 2 条点名要「真的执行了」。
# Duetto 那条 POST 从 query 读参数（index.mjs:361），所以走 params 不走 body。
@app.post("/api/piano/ncm/like", dependencies=[Depends(require_auth)])
async def piano_ncm_like(id: str = Query(max_length=64), like: bool = True) -> dict:
    try:
        return await piano.post(
            "/api/ncm/like", params={"id": id, "like": "1" if like else "0"}
        )
    except piano.PianoError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


# 网易云扫码登录。登录入口做在琴房里，不让她为了扫个码绕出去。
# 二维码和登录态都由 Duetto 持有，chatnest 只是转发——cookie 一步都不进小窝。

@app.get("/api/piano/ncm/qr", dependencies=[Depends(require_auth)])
async def piano_ncm_qr() -> dict:
    return await _piano("/api/ncm/qr")


@app.get("/api/piano/ncm/check", dependencies=[Depends(require_auth)])
async def piano_ncm_check(key: str = Query(max_length=256)) -> dict:
    return await _piano("/api/ncm/check", {"key": key})


@app.get("/api/piano/ncm/status", dependencies=[Depends(require_auth)])
async def piano_ncm_status() -> dict:
    return await _piano("/api/ncm/status")


@app.post("/api/piano/ncm/logout", dependencies=[Depends(require_auth)])
async def piano_ncm_logout() -> dict:
    return await _piano_post("/api/ncm/logout")


# ── 曲库：日推 / 排行 / 最近 / 红心 / 私人FM / 歌手 / 评论 ──────────────────
# 施工单 §3：把 index.mjs 的 /api/ncm/* 全部代理过来，一条不落。
# 这一节没有前端，是给后面三项（歌词页 / 房间 / 听歌档案）铺路。

@app.get("/api/piano/ncm/recommend", dependencies=[Depends(require_auth)])
async def piano_ncm_recommend() -> dict:
    return await _piano("/api/ncm/recommend")


@app.get("/api/piano/ncm/toplist", dependencies=[Depends(require_auth)])
async def piano_ncm_toplist(id: str = Query(default="", max_length=64)) -> dict:
    # 不带 id 返榜单列表，带 id 返那个榜的曲目（index.mjs:358 一条路由两种用法）
    return await _piano("/api/ncm/toplist", {"id": id})


@app.get("/api/piano/ncm/record", dependencies=[Depends(require_auth)])
async def piano_ncm_record() -> dict:
    return await _piano("/api/ncm/record")


@app.get("/api/piano/ncm/likelist", dependencies=[Depends(require_auth)])
async def piano_ncm_likelist() -> dict:
    return await _piano("/api/ncm/likelist")


@app.get("/api/piano/ncm/personal-fm", dependencies=[Depends(require_auth)])
async def piano_ncm_personal_fm() -> dict:
    return await _piano("/api/ncm/personal-fm")


@app.post("/api/piano/ncm/fm-trash", dependencies=[Depends(require_auth)])
async def piano_ncm_fm_trash(id: str = Query(max_length=64)) -> dict:
    return await _piano_post("/api/ncm/fm-trash", params={"id": id})


@app.get("/api/piano/ncm/search-artist", dependencies=[Depends(require_auth)])
async def piano_ncm_search_artist(kw: str = Query(max_length=200)) -> dict:
    return await _piano("/api/ncm/search-artist", {"kw": kw})


@app.get("/api/piano/ncm/artist-songs", dependencies=[Depends(require_auth)])
async def piano_ncm_artist_songs(id: str = Query(max_length=64)) -> dict:
    return await _piano("/api/ncm/artist-songs", {"id": id})


@app.get("/api/piano/ncm/comments", dependencies=[Depends(require_auth)])
async def piano_ncm_comments(id: str = Query(max_length=64)) -> dict:
    return await _piano("/api/ncm/comments", {"id": id})


# 歌单写操作。tracks 是逗号分隔的一串 id（批量操作靠它），
# 上限跟 Duetto 一次拉 300 首对齐，别让一条 URL 无限长。
@app.post("/api/piano/ncm/playlist-add", dependencies=[Depends(require_auth)])
async def piano_ncm_playlist_add(
    pid: str = Query(max_length=64),
    id: str = Query(max_length=4096),
) -> dict:
    return await _piano_post("/api/ncm/playlist-add", params={"pid": pid, "id": id})


@app.post("/api/piano/ncm/playlist-del", dependencies=[Depends(require_auth)])
async def piano_ncm_playlist_del(
    pid: str = Query(max_length=64),
    id: str = Query(max_length=4096),
) -> dict:
    return await _piano_post("/api/ncm/playlist-del", params={"pid": pid, "id": id})


# ── 档案：在场记录 / 听歌流水 / 房间时间线 ──────────────────────────────────
# 🔴 在场记录照旧存 Duetto（song_notes 表），但「每满 6 条揉成回忆」那一步
# 不走它的 maybeImpress——那段第一人称是 Duetto 自带的 DJ 写的，不是梁忱写的。
# 所以 Duetto 的 data/settings.json 里 ai.api_key 保持空着，maybeImpress
# 永远 return。回忆由小窝这条线揉，见施工单 §1.5（第 5 项要做的事）。

class PianoNoteBody(BaseModel):
    id: str = Field(default="", max_length=64)
    title: str = Field(default="", max_length=200)
    artist: str = Field(default="", max_length=200)
    cover: str = Field(default="", max_length=500)
    passage: str = Field(default="", max_length=200)     # 引用的那句歌词
    thought: str = Field(default="", max_length=1000)    # 她说的
    reply: str = Field(default="", max_length=2000)      # 他回的


@app.post("/api/piano/song-note", dependencies=[Depends(require_auth)])
async def piano_song_note(body: PianoNoteBody) -> dict:
    return await _piano_post("/api/song-note", body.model_dump())


class PianoListenBody(BaseModel):
    id: str = Field(default="", max_length=64)
    title: str = Field(default="", max_length=200)
    artist: str = Field(default="", max_length=200)
    cover: str = Field(default="", max_length=500)
    dur: int = 0


@app.post("/api/piano/listen-log", dependencies=[Depends(require_auth)])
async def piano_listen_log(body: PianoListenBody) -> dict:
    # 听歌档案的数据来源。以前一条都没写过，所以 listen-stats 一直是空的。
    return await _piano_post("/api/listen-log", body.model_dump())


@app.get("/api/piano/listen-stats", dependencies=[Depends(require_auth)])
async def piano_listen_stats() -> dict:
    return await _piano("/api/listen-stats")


# ── 房间实时同步：/ws 的代理 ────────────────────────────────────────────
# 前端连这条，小窝拿着 DUETTO_TOKEN 去连 Duetto 的 /ws。两条腿对拷字节。
#
# 🔴 鉴权必须自己写，两个原因，缺一个都是敞着门：
#   一、`Depends(require_auth)` 是个 Header 参数（main.py 上面那个），
#      WebSocket 路由上根本不生效。
#   二、AUTH_MODE=both 时那层 Basic Auth 是 `@app.middleware("http")`，
#      **WebSocket 不经过它**。所以这条路由是唯一一扇外层拦不住的门。
#
# 🔴 token 走 subprotocol，不走 query。浏览器的 new WebSocket() 不能设 header，
#   一般做法是塞 query，但小窝的 token 是 HMAC(CHAT_SECRET) 的**恒定值**
#   （auth.py:12），没有过期，进了反向代理的访问日志就是永久泄露。
#   subprotocol 在握手头里，日志不记。

@app.websocket("/ws/piano")
async def piano_ws(ws: WebSocket) -> None:
    raw = ws.headers.get("sec-websocket-protocol") or ""
    protos = [p.strip() for p in raw.split(",") if p.strip()]
    token = protos[1] if len(protos) >= 2 and protos[0] == "bearer" else ""
    if not token or not auth.verify_token(token):
        await ws.close(code=1008)          # policy violation
        return
    if not piano.configured():
        await ws.accept(subprotocol="bearer")
        await ws.close(code=1011, reason="piano not configured")
        return

    room = (ws.query_params.get("room") or "main")[:64]
    await ws.accept(subprotocol="bearer")

    try:
        async with piano.ws_connect(room) as up:
            logger.info("[琴房] 房间 %s 接上了", room)

            async def down_to_up() -> None:
                while True:
                    await up.send(await ws.receive_text())

            async def up_to_down() -> None:
                while True:
                    msg = await up.recv()
                    await ws.send_text(msg if isinstance(msg, str) else msg.decode())

            done, pending = await asyncio.wait(
                [asyncio.create_task(down_to_up()), asyncio.create_task(up_to_down())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            # 一头断了另一头就没意义了，别把任务漏在后台
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                    logger.info("[琴房] 房间 %s 断开：%s", room, exc)
    except piano.PianoError as exc:
        logger.warning("[琴房] WS 代理起不来：%s", exc)
    except Exception as exc:      # 上游连不上、握手被拒……都不该把小窝带崩
        logger.warning("[琴房] WS 上游连不上：%s: %s", exc.__class__.__name__, exc)
    finally:
        try:
            await ws.close()
        except RuntimeError:
            pass                  # 已经关了


@app.get("/api/piano/room/events", dependencies=[Depends(require_auth)])
async def piano_room_events(
    room: str = Query(default="main", max_length=64),
    limit: int = 120,
) -> dict:
    return await _piano("/api/room/events",
                        {"room": room, "limit": max(1, min(300, limit))})


@app.get("/api/background", dependencies=[Depends(require_auth)])
async def background_state() -> dict:
    return backgrounds.get_state()


@app.post("/api/background/{slot}", dependencies=[Depends(require_auth)])
async def background_upload(slot: str, file: UploadFile = File(...)) -> dict:
    if not backgrounds.valid_slot(slot):
        raise HTTPException(status_code=404, detail="unknown slot")
    if file.content_type not in backgrounds.ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail="unsupported image type")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > backgrounds.MAX_BYTES:
        raise HTTPException(status_code=413, detail="image too large")
    return backgrounds.store(slot, data)


@app.delete("/api/background/{slot}", dependencies=[Depends(require_auth)])
async def background_clear(slot: str) -> dict:
    if not backgrounds.valid_slot(slot):
        raise HTTPException(status_code=404, detail="unknown slot")
    return backgrounds.clear(slot)


@app.put("/api/background/mask", dependencies=[Depends(require_auth)])
async def background_mask(body: BackgroundMaskBody) -> dict:
    return backgrounds.set_mask(body.mask)


# 保留 require_auth：外层 outer_basic_auth 只在 AUTH_MODE=both 时启用（见 main.py 顶部
# OUTER_BASIC_AUTH_ENABLED），默认 AUTH_MODE=app 时它是空转的，不能当作访问控制。
# 前端不用 CSS url() 直连本路由，而是 fetch 带 Bearer 取回后转 blob: URL 再喂给 CSS 变量。
@app.get("/api/background/file/{slot}", dependencies=[Depends(require_auth)])
async def background_file(slot: str) -> FileResponse:
    if not backgrounds.valid_slot(slot):
        raise HTTPException(status_code=404, detail="unknown slot")
    path = backgrounds.file_path(slot)
    if path is None:
        raise HTTPException(status_code=404, detail="not set")
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )
