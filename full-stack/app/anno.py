"""一起看书（Anno）—— 原样接入 Shitsuten/anno-mcp，不改它一个字节。

anno 是一个独立的 Node 服务（Express 5，监听 127.0.0.1:3300），自带一整套
阅读器前端（划线高亮、夜间模式、字号、书签、全文搜索、生词本），另外通过
MCP 把「浏览书架 / 翻页 / 写批注 / 划线 / 书签」这几件事开放给梁忱。

🔴 为什么挂在 /marginalia 而不是别的路径：
   anno 的前端里写死了 `const API = '/marginalia/api'`（client/anno.js:1）。
   作者本来就是按「反向代理挂在 /marginalia 下」设计的。照着它挂，
   前端就一个字节都不用改；换个前缀就得去改人家的源码，那就不叫原封不动了。
   顺带一个好处：/marginalia/api 跟小窝自己的 /api/* 天然不撞车。

   GET  /marginalia/            → anno/client/index.html
   GET  /marginalia/anno.css    → anno/client/anno.css
   GET  /marginalia/anno.js     → anno/client/anno.js
   *    /marginalia/api/{path}  → 转发给 http://127.0.0.1:3300/api/{path}

🔴 anno 自己**没有任何鉴权**——谁摸到 /marginalia 谁就能翻她的书和批注，
   还能删。所以这道门必须由小窝来守。而 iframe 发不出 Authorization 头，
   所以这里认 cookie（登录时种的）或者 Bearer，两者认一个就放行。

🔴 请求和响应都走流式转发，不在内存里囤整本书。上传一本 PDF 动辄几十 MB，
   read() 进来会把常驻内存顶上去——队列 38 正在查小窝为什么涨到 189MiB，
   别再往上加。
"""

import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from . import auth

router = APIRouter()

# anno 的 Node 服务。它自己 listen 在 127.0.0.1，外面进不来，只能从这儿转。
ANNO_ORIGIN = os.environ.get("ANNO_ORIGIN", "http://127.0.0.1:3300")

# 前端静态文件。跟着仓库走（full-stack/anno/client），不是 /opt 下那份。
CLIENT_DIR = Path(__file__).resolve().parent.parent / "anno" / "client"

# 登录 cookie。iframe 里的请求带不了 Authorization，只能靠它。
COOKIE_NAME = "chat_auth"

# 白名单发静态文件。🔴 绝不要把 path 直接拼进 Path——那是目录穿越，
# 跟 main.py 里主屏图标那处一个道理。
_ASSETS = {
    "anno.css": "text/css; charset=utf-8",
    "anno.js": "application/javascript; charset=utf-8",
}

_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    # 跟 piano.py 一样复用一个连接池，别每次请求新建。
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=5.0))
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def authed(request: Request) -> bool:
    """cookie 或 Bearer，认一个就行。"""
    token = request.cookies.get(COOKIE_NAME, "")
    if token and auth.verify_token(token):
        return True
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return auth.verify_token(header[7:])
    return False


def _deny() -> Response:
    return Response(status_code=401, content="unauthorized")


@router.get("/marginalia")
@router.get("/marginalia/")
async def anno_index(request: Request) -> Response:
    if not authed(request):
        return _deny()
    return FileResponse(
        CLIENT_DIR / "index.html",
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/marginalia/{asset}")
async def anno_asset(asset: str, request: Request) -> Response:
    if not authed(request):
        return _deny()
    media_type = _ASSETS.get(asset)
    if media_type is None:
        return Response(status_code=404)
    return FileResponse(
        CLIENT_DIR / asset,
        media_type=media_type,
        headers={"Cache-Control": "no-cache"},
    )


# 逐跳头，不能原样转发（RFC 7230 §6.1）。content-length 也要摘掉：
# 流式转发之后长度会变，留着它浏览器会把响应截断。
_DROP_REQUEST = {"host", "connection", "keep-alive", "transfer-encoding", "upgrade", "cookie", "authorization"}
_DROP_RESPONSE = {"connection", "keep-alive", "transfer-encoding", "upgrade", "content-length", "content-encoding"}


@router.api_route(
    "/marginalia/api/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
)
async def anno_api(path: str, request: Request) -> Response:
    """把 /marginalia/api/* 原样转给 anno 的 /api/*。

    🔴 cookie 和 Authorization 不往下转（见 _DROP_REQUEST）——anno 那边不认，
       而且把小窝的登录令牌递给一个不需要它的服务没有任何好处。
    """
    if not authed(request):
        return _deny()

    client = await _get_client()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST}

    req = client.build_request(
        request.method,
        f"{ANNO_ORIGIN}/api/{path}",
        params=request.query_params,
        headers=headers,
        content=request.stream(),
    )
    try:
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError:
        # Node 没起来 / 崩了。别 500 到前端去，给一句人话。
        return Response(status_code=502, content="一起看书的服务没连上")

    out = {k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_RESPONSE}
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=out,
        # 🔴 发完必须 aclose，把连接还回池子。stream=True 的响应不关会一直
        #    占着连接，几十次之后连接池耗尽、后面的请求全部挂起。
        background=BackgroundTask(upstream.aclose),
    )
