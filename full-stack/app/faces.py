"""头像 + 朋友圈封面 + 个性签名。

为什么头像单独一个模块、而不是塞进朋友圈里：
    她的原话是「聊天有、以后的日记有、听歌有、朋友圈更要有」。
    头像一旦存在就是**全站共用**的一份身份——朋友圈换了聊天里也得换，
    不然就是两个人。所以它属于底层，不属于任何一个页面。

封面和签名只有朋友圈在用，但存储逻辑跟头像一模一样（上传一张图 / 存一段
短文本），分成两个模块只会重复一遍 tmp+replace 那套。放一起，slot 名字
把语义分开。

存 /data：持久卷（peek.py / backgrounds.py / appicon.py 都在用），
重新部署不会冲掉。
"""

import json
import os
import time
from pathlib import Path

STORE_DIR = Path(os.environ.get("FACES_DIR", "/data/faces")).expanduser()
META_PATH = STORE_DIR / "meta.json"

# 两个头像 + 一张朋友圈封面。
# 🔴 slot 名字别改：前端 URL、meta.json 里存的 key 都是它。
AVATAR_SLOTS = ("xiaoduo", "elian")
SLOTS = AVATAR_SLOTS + ("cover",)

MAX_BYTES = 5 * 1024 * 1024        # 前端已经压过，这里只兜底
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
SIGNATURE_MAX = 120

# 头像形状。她说圆的有点丑，要能自己切。
# 存在 meta 里而不是 localStorage：换个设备打开该是同一个样子。
SHAPES = ("circle", "rounded", "squircle")
DEFAULT_SHAPE = "rounded"


def valid_slot(slot: str) -> bool:
    return slot in SLOTS


def _path(slot: str) -> Path:
    return STORE_DIR / f"{slot}.jpg"


def _load_meta() -> dict:
    try:
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_meta(meta: dict) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = META_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(META_PATH)


def get_state() -> dict:
    """给前端的完整状态。

    v 是版本号，拼在图片 URL 后面做缓存刷新——不带它的话换了头像
    她要清缓存才看得见。
    """
    meta = _load_meta()
    out = {
        slot: {"set": _path(slot).is_file(), "v": int((meta.get(slot) or {}).get("v") or 0)}
        for slot in SLOTS
    }
    out["signature"] = str(meta.get("signature") or "")
    shape = str(meta.get("shape") or DEFAULT_SHAPE)
    out["shape"] = shape if shape in SHAPES else DEFAULT_SHAPE
    return out


def file_path(slot: str) -> Path | None:
    p = _path(slot)
    return p if p.is_file() else None


def store(slot: str, data: bytes) -> dict:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    # tmp + replace：中途断了不留半张图。半张图比没有图更难查——
    # 浏览器会缓存那个坏的。
    tmp = _path(slot).with_suffix(".jpg.tmp")
    tmp.write_bytes(data)
    tmp.replace(_path(slot))
    meta = _load_meta()
    meta[slot] = {"v": int(time.time())}
    _save_meta(meta)
    return get_state()


def clear(slot: str) -> dict:
    try:
        _path(slot).unlink()
    except OSError:
        pass
    meta = _load_meta()
    meta.pop(slot, None)
    _save_meta(meta)
    return get_state()


def set_shape(shape: str) -> dict:
    if shape not in SHAPES:
        raise ValueError("unknown shape")
    meta = _load_meta()
    meta["shape"] = shape
    _save_meta(meta)
    return get_state()


def set_signature(text: str) -> dict:
    meta = _load_meta()
    meta["signature"] = str(text or "").strip()[:SIGNATURE_MAX]
    _save_meta(meta)
    return get_state()
