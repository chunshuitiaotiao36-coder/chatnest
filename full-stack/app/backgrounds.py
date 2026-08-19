"""服务端背景图存储：/data/backgrounds/{light,dark}.jpg + meta.json"""
import json
import os
import time
from pathlib import Path

STORE_DIR = Path(os.environ.get("BACKGROUNDS_DIR", "/data/backgrounds")).expanduser()
META_PATH = STORE_DIR / "meta.json"

SLOTS = ("light", "dark")
MAX_BYTES = 5 * 1024 * 1024          # 前端已压到几百 KB，这里只是兜底
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
# 🔴 从 0.5 降到 0.18。原来那层灰正是她说的「上传了还是那个底色，很丑」——
# 半透明白盖在她的图上，图就成了背景噪点。要看清字的时候她自己往上拉。
DEFAULT_MASK = 0.18


def _blank() -> dict:
    return {"light": {"set": False, "v": 0}, "dark": {"set": False, "v": 0}, "mask": DEFAULT_MASK}


def _load() -> dict:
    state = _blank()
    try:
        raw = json.loads(META_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    for slot in SLOTS:
        s = raw.get(slot) or {}
        # 以磁盘上文件是否真实存在为准，meta 只提供版本号
        state[slot] = {"set": _path(slot).exists(), "v": int(s.get("v") or 0)}
    try:
        state["mask"] = min(0.9, max(0.0, float(raw.get("mask", DEFAULT_MASK))))
    except (TypeError, ValueError):
        state["mask"] = DEFAULT_MASK
    return state


def _save(state: dict) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = META_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(META_PATH)


def _path(slot: str) -> Path:
    return STORE_DIR / f"{slot}.jpg"


def valid_slot(slot: str) -> bool:
    return slot in SLOTS


def get_state() -> dict:
    return _load()


def file_path(slot: str) -> Path | None:
    p = _path(slot)
    return p if p.exists() else None


def store(slot: str, data: bytes) -> dict:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(slot).with_suffix(".jpg.tmp")
    tmp.write_bytes(data)
    tmp.replace(_path(slot))
    state = _load()
    state[slot] = {"set": True, "v": int(time.time())}
    _save(state)
    return state


def clear(slot: str) -> dict:
    _path(slot).unlink(missing_ok=True)
    state = _load()
    state[slot] = {"set": False, "v": 0}
    _save(state)
    return state


def set_mask(value: float) -> dict:
    state = _load()
    state["mask"] = min(0.9, max(0.0, float(value)))
    _save(state)
    return state
