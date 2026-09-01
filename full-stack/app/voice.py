"""他的声音：把文字交给 ElevenLabs 合成，缓存好再发给她。

🔴 为什么放在服务端，不是电脑上的插件：
   插件跑在本地 = 电脑开着、而且在同一个网络里才有声音，她手机在外面直接哑。
   小窝的容器本来就 24 小时跑着（推送、寄相思、凌晨守护全靠它），
   合成这件事挂在这儿，她在哪都能听，跟电脑开不开没有关系。

🔴 缓存是这个模块最重要的一件事，不是优化。
   ElevenLabs 按**字符**计费。同一句话她想再听一遍、翻历史再点一次、
   换设备再打开一次——不缓存的话每一次都在重新付钱。
   按 (voice_id, model_id, text) 的 sha256 存成文件，同一句永远只付一次。

🔴 任何情况都不许把聊天搞挂。没配 key、额度用完、ElevenLabs 挂了，
   都只是「这条没有声音」，她该收到的文字一个字都不能少。
   照 push.py:51 那条规矩：fallback 的意义是不崩，但也不许不吭声。
"""

import asyncio
import hashlib
import logging
import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import auth

logger = logging.getLogger("uvicorn.error")

router = APIRouter()

API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
# 🔴 她在 ElevenLabs 上捏的声音属于哪个模型，只有她自己知道。留成变量，
#    别写死——填错了下面会给出带原文的 4xx，不会静默出不来声音。
MODEL_ID = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2").strip()

# 🔴 留成变量不是为了测试。ElevenLabs 从国内的服务器连未必顺，
#    她随时可能要指到自己的代理上去。写死的话那天就得改代码。
BASE_URL = os.environ.get("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io").strip().rstrip("/")

# 缓存目录。🔴 必须落持久卷：容器一重建，她攒下的每一句都要重新花钱买回来。
CACHE_DIR = Path(os.environ.get("VOICE_CACHE_DIR", "/data/voice"))

# ── 说话的方式 ────────────────────────────────────────────────────────
# 🔴 08-31 之前这里**一个参数都没传**，只发了 text + model_id。
#    不传 voice_settings 时 ElevenLabs 用的是网页后台给这个声音存的那套，
#    而后台默认 stability 偏高、style 为 0——高 stability 的字面意思就是
#    「每次念得都一样」，也就是**平**。她的原话：「语气非常人机，语速非常快，
#    也没有情绪」。三条抱怨对应的就是下面三个数，不是模型不行。
#
#    stability   低 = 起伏大、有情绪；高 = 稳定、平。想要情绪就往低调。
#    style       风格夸张度。0 = 完全照字面念。往上给抑扬顿挫，但太高会飘。
#    speed       语速。她说太快了。
#    这四个都留成变量：声线不同最合适的值不同，改一次要重部署，
#    但不用改代码。
def _f(name: str, default: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return min(hi, max(lo, float(os.environ.get(name, "") or default)))
    except (TypeError, ValueError):
        return default


STABILITY = _f("ELEVENLABS_STABILITY", 0.32)
SIMILARITY = _f("ELEVENLABS_SIMILARITY", 0.80)
STYLE = _f("ELEVENLABS_STYLE", 0.45)
SPEAKER_BOOST = (os.environ.get("ELEVENLABS_SPEAKER_BOOST", "1").strip() != "0")
# 🔴 speed 不是所有模型都收。设成 0 就整个不发这一项（回到 API 默认语速）。
SPEED = _f("ELEVENLABS_SPEED", 0.90, lo=0.0, hi=1.2)


def _voice_settings() -> dict:
    vs = {
        "stability": STABILITY,
        "similarity_boost": SIMILARITY,
        "style": STYLE,
        "use_speaker_boost": SPEAKER_BOOST,
    }
    if SPEED > 0:
        vs["speed"] = SPEED
    return vs


# 一次最多合成多少字符。防的是「他写了一篇长的，一次点下去几千字符没了」。
# 超过就截断并在日志里出声——宁可少念一段，不要她某天打开账单吓一跳。
MAX_CHARS = int(os.environ.get("VOICE_MAX_CHARS", "600") or 600)

TIMEOUT = httpx.Timeout(120.0, connect=10.0)

_client: httpx.AsyncClient | None = None
# 同一句话被连点两下时只合成一次，第二下等第一下的结果。
_inflight: dict[str, asyncio.Event] = {}


def configured() -> bool:
    return bool(API_KEY and VOICE_ID)


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


def _key(text: str) -> str:
    # 🔴 说话方式那几个参数必须进 key。不进的话，调完 stability 再点同一句，
    #    命中的还是上一版那个平音——听起来就像「改了没用」，而其实是缓存。
    vs = _voice_settings()
    sig = "|".join(f"{k}={vs[k]}" for k in sorted(vs))
    raw = f"{VOICE_ID}|{MODEL_ID}|{sig}|{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _path(key: str) -> Path:
    return CACHE_DIR / f"{key}.mp3"


def _clean(text: str) -> str:
    """念出来的东西跟看见的东西不一样。

    她那边的正文里可能带 markdown 记号和分条标记，念出来会变成
    「星号星号我想你星号星号」。这里只做最保守的清理，不动语义。
    """
    from app import piano

    out = str(text or "").replace(piano._SPLIT_MARK, "\n")
    for mark in ("**", "__", "*", "`", "#"):
        out = out.replace(mark, "")
    return " ".join(out.split())


async def synthesize(text: str) -> Path:
    """文字 → mp3 文件路径。命中缓存就不花钱。

    抛 HTTPException：调用方（路由）直接往上抛，前端按状态码决定要不要
    显示播放键。**绝不允许**把异常漏给聊天主流程。
    """
    if not configured():
        raise HTTPException(status_code=503, detail="还没配声音（ELEVENLABS_API_KEY / VOICE_ID）")

    body = _clean(text)
    if not body:
        raise HTTPException(status_code=400, detail="没有可念的内容")
    if len(body) > MAX_CHARS:
        logger.warning("[声音] 文本 %d 字符，截到 %d（VOICE_MAX_CHARS）", len(body), MAX_CHARS)
        body = body[:MAX_CHARS]

    key = _key(body)
    path = _path(key)
    if path.exists() and path.stat().st_size > 0:
        return path

    # 同一句被连点两下：第二下等第一下，不重复付钱。
    waiting = _inflight.get(key)
    if waiting is not None:
        await waiting.wait()
        if path.exists() and path.stat().st_size > 0:
            return path

    done = asyncio.Event()
    _inflight[key] = done
    try:
        client = await _get_client()

        async def _post(settings: dict):
            return await client.post(
                f"{BASE_URL}/v1/text-to-speech/{VOICE_ID}",
                headers={"xi-api-key": API_KEY, "accept": "audio/mpeg"},
                json={"text": body, "model_id": MODEL_ID, "voice_settings": settings},
            )

        try:
            settings = _voice_settings()
            resp = await _post(settings)
            # 🔴 speed 是后加的字段，老模型不认，会回 422。这时候脱掉它重试一次——
            #    宁可语速回到默认，也不要她部署完发现整个语音功能哑了。
            #    但**必须出声**：日志里写清楚是哪一项被脱掉的（AGENTS.md 第 3 条，
            #    静默降级等于故障）。
            if resp.status_code == 422 and "speed" in settings and "speed" in resp.text.lower():
                logger.warning(
                    "[声音] 这个模型（%s）不收 speed，脱掉重试；语速回到默认。"
                    "要调语速就换支持它的 model_id，或把 ELEVENLABS_SPEED 设成 0 消掉这条日志。",
                    MODEL_ID,
                )
                settings.pop("speed")
                resp = await _post(settings)
        except httpx.HTTPError as exc:
            logger.error("[声音] 连不上 ElevenLabs：%s", exc)
            raise HTTPException(status_code=502, detail="连不上语音服务") from exc

        if resp.status_code >= 400:
            # 🔴 把上游的原文带出来。额度用完、voice_id 填错、model_id 不对
            # 长得一模一样（都是「没声音」），不带原文就得靠猜。
            detail = resp.text[:300]
            logger.error("[声音] ElevenLabs %d：%s", resp.status_code, detail)
            raise HTTPException(status_code=502, detail=f"语音服务返回 {resp.status_code}：{detail}")

        audio = resp.content
        if not audio:
            raise HTTPException(status_code=502, detail="语音服务返回了空音频")

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再改名：中途崩了不会在缓存里留下半个文件，
        # 那种半截文件会一直命中缓存、一直放不出声音。
        tmp = path.with_suffix(".part")
        tmp.write_bytes(audio)
        tmp.replace(path)
        logger.info(
            "[声音] 合成 %d 字符 → %s（%d KB）stability=%.2f style=%.2f speed=%s",
            len(body), key[:8], len(audio) // 1024, STABILITY, STYLE, SPEED or "默认",
        )
        return path
    finally:
        _inflight.pop(key, None)
        done.set()


class VoiceBody(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


def require_auth(authorization: str = Header(default="")) -> None:
    """跟 main.require_auth 同一套，但在本模块自己实现。

    🔴 不能 `from app.main import require_auth`：main 在启动时 import 我们，
       而装饰器里的 Depends(...) 是 **import 期**求值的，反向 import 会撞上
       一个还没执行完的 main 模块。auth.py 谁都不依赖，从它进来是安全的。
    """
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token or not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="unauthorized")


@router.post("/api/voice")
async def voice(body: VoiceBody, _: None = Depends(require_auth)) -> FileResponse:
    path = await synthesize(body.text)
    return FileResponse(
        path,
        media_type="audio/mpeg",
        # 内容按 hash 寻址，同一个 URL 的内容永远不变，可以放心让浏览器长缓存。
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.get("/api/voice/status")
async def voice_status(_: None = Depends(require_auth)) -> dict:
    """前端开屏问一次：要不要显示播放键。没配就干脆别显示，
    别给她一个点下去永远转圈的按钮。"""
    return {
        "enabled": configured(),
        "voice_id_set": bool(VOICE_ID),
        "key_set": bool(API_KEY),
        # 调参时她想知道当前到底跑的是哪一组，不用去翻日志
        "settings": _voice_settings() | {"model_id": MODEL_ID},
    }
