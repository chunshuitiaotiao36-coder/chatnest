"""一起看书（Anno）—— 原样接入 Shitsuten/anno-mcp，不改它一个字节。

anno 是一个独立的 Node 服务（Express 5，监听 127.0.0.1:3300），自带一整套
阅读器前端（划线高亮、夜间模式、字号、书签、全文搜索、生词本），另外通过
MCP 把「浏览书架 / 翻页 / 写批注 / 划线 / 书签」这几件事开放给梁忱。

🔴 为什么挂在 /marginalia 而不是别的路径：
   两条独立的证据都指向它。一是 anno 的前端里写死了
   `const API = '/marginalia/api'`（client/anno.js:1）；二是它的 MCP 资源
   元数据里返回的 resource 是 `${baseUrl}/marginalia/mcp`
   （server.mjs 的 mcpResourceMetadata）。作者本来就是按「反向代理挂在
   /marginalia 下」设计的。照着它挂，前端和 MCP 都一个字节不用改；换个前缀
   就得去改人家源码，那就不叫原封不动了。
   顺带一个好处：/marginalia/api 跟小窝自己的 /api/* 天然不撞车。

这个模块挂三组路由：

  一、阅读器（书房第五栏那个 iframe）—— 走小窝的登录
      GET  /marginalia/            → anno/client/index.html
      GET  /marginalia/anno.{css,js}
      *    /marginalia/api/{path}  → 转发 127.0.0.1:3300/api/{path}
      GET  /marginalia/health      → 转发，看 Node 死没死

  二、对外 MCP（可选，默认关，见 ANNO_PUBLIC_MCP）—— 走 anno 自己的令牌
      让 Claude Desktop / claude.ai 这类外部 MCP 客户端能直连她的书。
      *    /marginalia/mcp{path}
      根路径那几个是 OAuth 发现要求的位置，不能挪：
      GET  /.well-known/oauth-protected-resource[/mcp]
      GET  /.well-known/oauth-authorization-server
      POST /register   GET /authorize   POST /token

🔴 anno 自己**对 /api 没有任何鉴权**——谁摸到 /marginalia 谁就能翻她的书和
   批注，还能删。所以那道门必须由小窝来守。而 iframe 发不出 Authorization
   头，所以这里认 cookie（登录时种的）或者 Bearer，两者认一个就放行。

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

# ── 对外 MCP 开关 ────────────────────────────────────────────────────
# 只有想让 Claude Desktop / claude.ai 这类外部客户端直连她的书时才需要开。
# 书房里的「一起看书」不需要它——梁忱是从小窝后端走 127.0.0.1 连的。
#
# 🔴 两个条件同时满足才真的挂上去，缺一个就当没开：
#    1. ANNO_PUBLIC_MCP=1
#    2. ANNO_MCP_TOKEN 非空
#    第二条是硬性的。anno 的 mcpAuthorized() 在 MCP_AUTH_TOKEN 为空时
#    **直接返回 true**（server.mjs:648）——那意味着不带令牌就挂到公网上，
#    等于把她整个批注库对全世界敞开写。宁可不挂。
ANNO_PUBLIC_MCP = os.environ.get("ANNO_PUBLIC_MCP", "").strip() == "1"
ANNO_MCP_TOKEN = os.environ.get("ANNO_MCP_TOKEN", "").strip()
PUBLIC_MCP_ON = ANNO_PUBLIC_MCP and bool(ANNO_MCP_TOKEN)

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


# 逐跳头，不能原样转发（RFC 7230 §6.1）。content-length 也要摘掉：
# 流式转发之后长度会变，留着它浏览器会把响应截断。
_DROP_RESPONSE = {"connection", "keep-alive", "transfer-encoding", "upgrade", "content-length", "content-encoding"}
# 转发请求时丢掉的：逐跳头，加上小窝自己的登录凭证——anno 不认，
# 而且把登录令牌递给一个不需要它的服务没有任何好处。
_DROP_REQUEST = {"host", "connection", "keep-alive", "transfer-encoding", "upgrade", "cookie", "authorization"}


async def _forward(request: Request, upstream_path: str, *, keep_auth: bool = False) -> Response:
    """把当前请求原样转给 anno 的 upstream_path，请求响应都流式。

    keep_auth=True 时保留 Authorization 头——对外 MCP 那组要靠它把客户端的
    Bearer 令牌递给 anno 自己的 mcpAuthorized() 去校验。
    """
    client = await _get_client()
    drop = _DROP_REQUEST - {"authorization"} if keep_auth else _DROP_REQUEST
    headers = {k: v for k, v in request.headers.items() if k.lower() not in drop}

    # 🔴 anno 的 externalBaseUrl() 靠这两个头拼出它对外的地址，OAuth 发现
    #    文档和 401 里的 resource_metadata URL 全指望它。反代后面不补的话，
    #    它会把 127.0.0.1:3300 当成对外地址发给客户端。
    headers["x-forwarded-proto"] = request.headers.get(
        "x-forwarded-proto", request.url.scheme
    ).split(",", 1)[0].strip()
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if forwarded_host:
        headers["x-forwarded-host"] = forwarded_host

    req = client.build_request(
        request.method,
        f"{ANNO_ORIGIN}{upstream_path}",
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


# ══════════════════════════════════════════════════════════════════
# 一、阅读器 —— 走小窝的登录
# ══════════════════════════════════════════════════════════════════


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


@router.get("/marginalia/health")
async def anno_health(request: Request) -> Response:
    if not authed(request):
        return _deny()
    return await _forward(request, "/health")


@router.api_route(
    "/marginalia/api/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
)
async def anno_api(path: str, request: Request) -> Response:
    if not authed(request):
        return _deny()
    return await _forward(request, f"/api/{path}")


# ══════════════════════════════════════════════════════════════════
# 二、对外 MCP —— 走 anno 自己的令牌，默认不挂
# ══════════════════════════════════════════════════════════════════

if PUBLIC_MCP_ON:

    @router.api_route(
        "/marginalia/mcp{path:path}",
        methods=["GET", "POST", "DELETE", "OPTIONS"],
    )
    async def anno_mcp(path: str, request: Request) -> Response:
        # 不查小窝的登录：外部 MCP 客户端没有她的 cookie。
        # 这里放行到 anno，由它的 mcpAuthorized() 校验 Bearer。
        # 上面 PUBLIC_MCP_ON 已经保证了 anno 那边的令牌非空。
        return await _forward(request, f"/mcp{path}", keep_auth=True)

    # OAuth 发现文档必须待在源的根路径上（RFC 9728 / RFC 8414），
    # 挪到 /marginalia 下面客户端就找不着了。
    @router.get("/.well-known/oauth-protected-resource")
    @router.get("/.well-known/oauth-protected-resource/mcp")
    @router.get("/.well-known/oauth-authorization-server")
    async def anno_oauth_discovery(request: Request) -> Response:
        return await _forward(request, request.url.path, keep_auth=True)

    @router.api_route("/register", methods=["POST"])
    @router.api_route("/authorize", methods=["GET"])
    @router.api_route("/token", methods=["POST"])
    async def anno_oauth_flow(request: Request) -> Response:
        return await _forward(request, request.url.path, keep_auth=True)


# ══════════════════════════════════════════════════════════════════
# 三、垫底：阅读器的静态资源
# ══════════════════════════════════════════════════════════════════


@router.get("/marginalia/{asset}")
async def anno_asset(asset: str, request: Request) -> Response:
    # 🔴 放在 /marginalia/api 和 /marginalia/mcp 后面注册：FastAPI 按声明顺序
    #    匹配，这条 {asset} 会吞掉同层级的一切，必须垫底。
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
