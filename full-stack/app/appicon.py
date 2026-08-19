"""主屏图标：她自己画的那张，存 /data/icons/。

为什么不塞进仓库：她不想为了换一张图去登 GitHub。图标是**她的东西**，
换起来该像换背景图一样，在手机上点两下就完事。

为什么存 /data：那是持久卷（peek.py 和 backgrounds.py 都在用），
重新部署不会把图冲掉。仓库里的 static/icons/ 仍然保留作为兜底，
两边都没有的时候路由 404，head 里那个 SVG 顶上——行为跟没有这套东西一样。

🔴 必须是 PNG。iOS 的 apple-touch-icon 不支持 SVG：塞 SVG 会被静默忽略，
然后 iOS 拿站名首字母画一个方块顶上（主屏那个丑 N 就是这么来的）。
所以这里不接受 SVG，前端也会把任何图转成 PNG 再传。
"""

import json
import os
import time
from pathlib import Path

STORE_DIR = Path(os.environ.get("APPICON_DIR", "/data/icons")).expanduser()
META_PATH = STORE_DIR / "meta.json"

# iOS 只用 180。另两个留给 Android / 启动图，她用不到但接口一致。
SIZES = (180, 192, 512)
MAX_BYTES = 2 * 1024 * 1024
# 只收 PNG，理由见模块开头
ALLOWED_TYPES = {"image/png"}


def _path(size: int) -> Path:
    return STORE_DIR / f"nepeta-{size}.png"


def valid_size(size: int) -> bool:
    return size in SIZES


def _load() -> dict:
    try:
        raw = json.loads(META_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    # 以磁盘上文件真实存在为准，meta 只提供版本号（给缓存刷新用）
    return {
        "set": _path(180).is_file(),
        "v": int(raw.get("v") or 0),
        "sizes": [s for s in SIZES if _path(s).is_file()],
    }


def get_state() -> dict:
    return _load()


def file_path(size: int) -> Path | None:
    """先看持久卷，再回落到仓库里那份。

    仓库那份是给「想把图标钉进版本库」留的口子，目前是空的。
    """
    p = _path(size)
    if p.is_file():
        return p
    repo = Path(__file__).resolve().parent.parent / "static" / "icons" / f"nepeta-{size}.png"
    return repo if repo.is_file() else None


def store(size: int, data: bytes) -> dict:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再 replace：中途断了不会留下半张图，
    # 而半张图会让 iOS 缓存一个坏图标，比没有更难修。
    tmp = _path(size).with_suffix(".png.tmp")
    tmp.write_bytes(data)
    tmp.replace(_path(size))
    state = _load()
    state["v"] = int(time.time())
    tmp_meta = META_PATH.with_suffix(".json.tmp")
    tmp_meta.write_text(json.dumps({"v": state["v"]}, ensure_ascii=False), encoding="utf-8")
    tmp_meta.replace(META_PATH)
    return _load() | {"v": state["v"]}


def clear() -> dict:
    for s in SIZES:
        try:
            _path(s).unlink()
        except OSError:
            pass
    try:
        META_PATH.unlink()
    except OSError:
        pass
    return _load()
