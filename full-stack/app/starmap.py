"""记忆星图数据源：代理 Ombre /api/buckets，精简字段后给前端。"""
import json
import os
import time
import urllib.error
import urllib.request

CACHE_TTL = 300          # 5 分钟。记忆不是每秒都在变，别把 Ombre 打疼了
TIMEOUT = 20
PREVIEW_MAX = 220

_cache: dict = {"at": 0.0, "data": None}


def _api_base() -> str:
    """从 OMBRE_MCP_URL 推导 API 根地址；允许 OMBRE_API_BASE 显式覆盖。"""
    explicit = os.environ.get("OMBRE_API_BASE", "").strip()
    if explicit:
        return explicit.rstrip("/")
    url = os.environ.get("OMBRE_MCP_URL", "").strip().rstrip("/")
    if url.endswith("/mcp"):
        url = url[: -len("/mcp")]
    return url


def _slim(b: dict) -> dict:
    preview = (b.get("content_preview") or b.get("summary") or "").strip()
    if len(preview) > PREVIEW_MAX:
        preview = preview[:PREVIEW_MAX] + "…"
    return {
        "id": b.get("id") or "",
        "name": (b.get("name") or "").strip(),
        "type": b.get("type") or "",
        "importance": int(b.get("importance") or 0),
        "valence": float(b.get("valence") or 0.0),
        "arousal": float(b.get("arousal") or 0.0),
        "pinned": bool(b.get("pinned")),
        "highlight": bool(b.get("highlight")),
        "created": b.get("created") or "",
        "preview": preview,
    }


def fetch_stars(force: bool = False) -> dict:
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["at"] < CACHE_TTL:
        return _cache["data"]

    base = _api_base()
    token = os.environ.get("OMBRE_MCP_TOKEN", "")
    if not base:
        return {"stars": [], "count": 0, "error": "ombre_not_configured"}

    req = urllib.request.Request(f"{base}/api/buckets")
    if token:
        req.add_header("X-Admin-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, TimeoutError):
        # 拿不到就返回上一次的缓存，前端不至于白屏
        if _cache["data"] is not None:
            return _cache["data"]
        return {"stars": [], "count": 0, "error": "ombre_unreachable"}

    items = raw if isinstance(raw, list) else (raw.get("buckets") or raw.get("items") or [])
    stars = [_slim(b) for b in items if isinstance(b, dict)]
    stars.sort(key=lambda s: s["created"])          # 老的在前 → 螺旋中心
    data = {"stars": stars, "count": len(stars)}
    _cache["at"] = now
    _cache["data"] = data
    return data
