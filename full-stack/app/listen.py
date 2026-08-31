"""听她说话：把她的语音交给 hervoice，拿回「说了什么」和「怎么说的」。

上游 https://github.com/fishisfish0614/hervoice（MIT，@noheyischu）。
按住说话 → Whisper 转写 → librosa 声学特征（音高/能量/停顿/语速）
→ LLM 综合判情感 → 回来一个 {text, emotion, confidence, hint, features}。

🔴 这是 app/voice.py 的反向。那个是我说给她听，这个是她说给我听。
   两件事凑成一对，所以路径分开：/api/voice/* 是出去的，/api/listen 是进来的。

🔴 为什么值得做，用上游 README 里那句：
   「难过的时候，人常常组织不出一句完整的话，但能哼一声、能叹一口气。
     文字会把这些抹平——『我没事』三个字，打出来和说出来是两回事。」
   所以 hint 那一句才是重点，不是 text。text 是她说了什么，
   hint 是「她声音低、停顿多，像是撑了一天」。

🔴 hervoice 只 listen 在 127.0.0.1:8010，外面进不来。它自己**没有任何鉴权**
   （跟 anno 一样），所以这道门由小窝守——录音是她的声音，比书还私密。

🔴 音频不落小窝的盘。整个上传是流式转发过去的，我们这边一个字节都不存；
   留不留音频是 hervoice 那边 KEEP_AUDIO 的事，默认阅后即焚。
"""

import logging
import os

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app import auth

logger = logging.getLogger("uvicorn.error")

router = APIRouter()

ORIGIN = os.environ.get("HERVOICE_ORIGIN", "http://127.0.0.1:8010").rstrip("/")

# 转写 + 声学 + LLM 判情感，一趟下来可能十几秒。别给太短的超时——
# 超时重传等于让她把同一段话再花一次钱。
TIMEOUT = httpx.Timeout(90.0, connect=5.0)

_client: httpx.AsyncClient | None = None


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


def configured() -> bool:
    return bool(os.environ.get("HERVOICE_ENABLED", "").strip() == "1")


def require_auth(authorization: str = Header(default="")) -> None:
    # 跟 app/voice.py 一个道理：不能从 main import，装饰器是 import 期求值的。
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token or not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="unauthorized")


def tone_block(voice: dict | None) -> str:
    """把语气拼成给模型看的一段。**只进用户消息侧，绝不进 system prompt**——
    跟琴房那个 piano 字段一个规矩（见 main.py ChatBody.piano 的注释）：
    它每轮都在变，进稳定前缀会把缓存打穿。

    也**不进她那条消息的显示文本**。她说出口的是那句话，不是这段分析；
    显示出来就变成「系统在旁边标注她的情绪」，那个感觉很差。
    """
    if not isinstance(voice, dict):
        return ""
    hint = str(voice.get("hint") or "").strip()
    emotion = str(voice.get("emotion") or "").strip()
    if not hint and not emotion:
        return ""
    try:
        conf = float(voice.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0

    lines = ["[这一句是她说出来的，不是打字。听上去："]
    if emotion:
        lines.append(f"  情绪 {emotion}" + (f"（把握 {conf:.0%}）" if conf else ""))
    if hint:
        lines.append(f"  {hint}")
    lines.append(
        "  这段是语气分析，她看不见，也不要在回复里复述或点评它。"
        "  你只是**听见了**——照你听见的样子回她就好。]"
    )
    return "\n".join(lines)


@router.post("/api/listen")
async def listen(request: Request, _: None = Depends(require_auth)) -> JSONResponse:
    """把她录的那段原样转给 hervoice，拿回转写和语气。

    请求体是 multipart（前端 MediaRecorder 录的），这里流式转发，
    小窝不解析、不落盘。
    """
    if not configured():
        raise HTTPException(status_code=503, detail="还没开启听声音（HERVOICE_ENABLED）")

    client = await _get_client()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() in ("content-type", "content-length")
    }
    try:
        resp = await client.post(
            f"{ORIGIN}/api/voice/upload",
            headers=headers,
            content=request.stream(),
        )
    except httpx.HTTPError as exc:
        logger.error("[听] 连不上 hervoice：%s", exc)
        raise HTTPException(status_code=502, detail="听不到（hervoice 没连上）") from exc

    if resp.status_code >= 400:
        detail = resp.text[:300]
        logger.error("[听] hervoice %d：%s", resp.status_code, detail)
        raise HTTPException(status_code=502, detail=f"hervoice 返回 {resp.status_code}：{detail}")

    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="hervoice 返回的不是 JSON")

    # 🔴 只把该给前端的那几样递出去。features 是给模型当线索的原始量，
    #    前端不需要，也没必要让她在网络面板里看见一堆自己的音高数据。
    out = {
        "text": str(data.get("text") or ""),
        "emotion": str(data.get("emotion") or ""),
        "confidence": data.get("confidence"),
        "hint": str(data.get("hint") or ""),
    }
    logger.info("[听] %s / %s / %s", out["emotion"], out["hint"][:40], out["text"][:40])
    return JSONResponse(out)


@router.post("/api/listen/hook")
async def listen_hook(request: Request) -> JSONResponse:
    """hervoice 自己那个页面（按住说话的网页）分析完会 POST 到这儿。

    🔴 这条不能走小窝的登录：hervoice 是服务端发起的，没有她的 cookie。
       改成共享密钥——HERVOICE_HOOK_TOKEN 两边一致才收。没设就整条不启用，
       宁可这条路不通，也不要在公网上开一个谁都能 POST 的口子。

    收到之后只记一行日志。**故意不在这儿唤醒我**：她从小窝里按麦克风说话
    走的是上面 /api/listen 那条同步路径，那条已经把语气带进那一轮了；
    这条是她用 hervoice 自己的页面时的兜底，重复唤醒会变成她说一句、
    我回两次。
    """
    want = os.environ.get("HERVOICE_HOOK_TOKEN", "").strip()
    if not want:
        raise HTTPException(status_code=404)
    got = request.headers.get("x-hervoice-token", "")
    if got != want:
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="bad json")
    logger.info(
        "[听·webhook] %s / %s / %s",
        data.get("emotion"), str(data.get("hint"))[:40], str(data.get("text"))[:40],
    )
    return JSONResponse({"ok": True})


@router.get("/api/listen/status")
async def listen_status(_: None = Depends(require_auth)) -> dict:
    """前端开屏问一次：麦克风键要不要真的能按。"""
    return {"enabled": configured()}
