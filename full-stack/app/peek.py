"""窥屏：让梁忱在开口前看一眼小朵的屏幕。

🔴 没配 SMTP 环境变量时整块静默失效，凌晨守护照常开口——
   照 claude.py:64-65 那套，可选依赖绝不许拖垮主流程。

链路：发一封触发邮件到她的 iCloud → 手机上「收到邮件」自动化触发截屏 →
POST /api/peek 上传 → 这边轮询等新图 → 拿到就把路径拼进 prompt。

🔴 走 iCloud 不是 Gmail：08-13 实测她的 Google 账号开不了应用专用密码
   （入口直接返回「您的账号不支持您正在尝试的设置」）。iCloud 自己发给自己，
   顺带也不会被当垃圾邮件拦掉。
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

# 借 uvicorn.error：全仓库没有 logging 配置，root logger 默认 WARNING 会把 .info() 吞掉
peek_logger = logging.getLogger("uvicorn.error")

ROOT = Path(
    os.environ.get("AGENT_APP_ROOT", Path(__file__).resolve().parent.parent)
).expanduser().resolve()
# 照 uploads.py 的目录习惯（UPLOAD_ROOT = ROOT / "uploads"）另开一个子目录。
# 线上 AGENT_APP_ROOT=/data 是持久卷，重新部署不会把图冲掉。
PEEK_ROOT = ROOT / "peeks"
MAX_KEEP = 10

SMTP_HOST = os.environ.get("PEEK_SMTP_HOST", "smtp.mail.me.com")
SMTP_PORT = os.environ.get("PEEK_SMTP_PORT", "587")
SMTP_MODE = os.environ.get("PEEK_SMTP_MODE", "starttls").strip().lower()
SMTP_USER = os.environ.get("PEEK_SMTP_USER", "")
SMTP_PASS = os.environ.get("PEEK_SMTP_PASS", "")
MAIL_TO = os.environ.get("PEEK_MAIL_TO", "")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        peek_logger.warning("[窥屏] %s 不是整数，退回默认值 %s", name, default)
        return default


def smtp_port() -> int:
    try:
        return int(SMTP_PORT)
    except (TypeError, ValueError):
        peek_logger.warning("[窥屏] PEEK_SMTP_PORT=%r 不是整数，退回 587", SMTP_PORT)
        return 587


def max_bytes() -> int:
    return _env_int("PEEK_MAX_BYTES", 10 * 1024 * 1024)


def configured() -> bool:
    """MAIL_TO 也算在内：没有收件人这封信根本发不出去，
    少了它整条链路会静默失效——而静默失效正是这几张单子最恨的东西。"""
    return bool(SMTP_HOST and SMTP_PORT and SMTP_MODE and SMTP_USER and SMTP_PASS and MAIL_TO)


# ---------- 截图落盘 ---------------------------------------------------------


def _shots() -> list[Path]:
    if not PEEK_ROOT.is_dir():
        return []
    return sorted(
        (p for p in PEEK_ROOT.glob("peek-*.png") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )


def _prune() -> None:
    """只留最近 10 张。"""
    shots = _shots()
    for old in shots[:-MAX_KEEP]:
        try:
            old.unlink()
        except OSError:
            peek_logger.warning("[窥屏] 删不掉旧图 %s", old)


def save_shot(data: bytes) -> Path:
    PEEK_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    path = PEEK_ROOT / f"peek-{stamp}.png"
    path.write_bytes(data)
    _prune()
    peek_logger.info("[窥屏] 收到截图 %s（%d 字节）", path.name, len(data))
    return path


def _latest_shot() -> Path | None:
    shots = _shots()
    return shots[-1] if shots else None


def _age_sec(path: Path) -> float:
    return time.time() - path.stat().st_mtime


def _latest_shot_after(t0: float) -> Path | None:
    shot = _latest_shot()
    if shot and shot.stat().st_mtime >= t0:
        return shot
    return None


# ---------- 发信 -------------------------------------------------------------


def _send(host: str, port: int, mode: str, user: str, password: str,
          to: str, subject: str, body: str) -> None:
    """🔴 smtplib 在函数内 import，不许放模块顶部——跟 pywebpush 一个纪律。
    🔴 timeout 必须给：不给的话 SMTP 卡住会把那个后台任务永久挂死。
    """
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if mode == "ssl":
        with smtplib.SMTP_SSL(host, port, timeout=20) as s:
            s.login(user, password)
            s.send_message(msg)
    else:  # starttls
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)


def _trigger_body() -> str:
    # 正文带时间戳，免得邮件客户端把多封折叠成同一封（折叠了自动化只触发一次）
    return f"peek {datetime.now(timezone.utc).isoformat()}"


async def send_trigger_mail() -> dict:
    """发一封触发邮件。返回 {"ok", "error", "host", "port", "mode"}，**不抛。**"""
    info = {"ok": False, "error": None, "host": SMTP_HOST,
            "port": smtp_port(), "mode": SMTP_MODE}
    if not configured():
        info["error"] = "unconfigured"
        return info
    try:
        # 🔴 smtplib 是阻塞的，必须 to_thread 包起来——跟 pywebpush 一个待遇。
        #    单进程 uvicorn，直接调会卡住整个事件循环。
        await asyncio.to_thread(
            _send, SMTP_HOST, smtp_port(), SMTP_MODE, SMTP_USER, SMTP_PASS,
            MAIL_TO, "PEEK", _trigger_body(),
        )
        info["ok"] = True
    except Exception as exc:
        peek_logger.warning("[窥屏] 触发邮件发不出去 %s: %s", type(exc).__name__, exc)
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


# ---------- 主流程 -----------------------------------------------------------


async def capture(timeout: int | None = None) -> Path | None:
    """要一张此刻的屏幕。拿不到返回 None，**绝不抛、绝不返回过期图**。

    🔴 拿十小时前的屏当现在，比看不见坏得多——梁忱会对着一张早就关掉的页面
       说话，她当场就知道这套东西是坏的。
    """
    if not configured():
        return None
    wait = timeout if timeout is not None else _env_int("PEEK_WAIT_SEC", 45)
    fresh = _env_int("PEEK_FRESH_SEC", 60)
    try:
        latest = _latest_shot()
        if latest and _age_sec(latest) < fresh:
            peek_logger.info("[窥屏] 刚有一张新图，直接用 %s", latest.name)
            return latest

        t0 = time.time()
        mail = await send_trigger_mail()
        if not mail["ok"]:
            return None
        while time.time() - t0 < wait:
            await asyncio.sleep(1.5)
            shot = _latest_shot_after(t0)
            if shot:
                peek_logger.info("[窥屏] 等到新图 %s（%.1f 秒）", shot.name, time.time() - t0)
                return shot
        peek_logger.info("[窥屏] 等了 %s 秒没等到，这次不看图照常开口", wait)
        return None
    except Exception:
        peek_logger.exception("[窥屏] 取图失败（不影响开口）")
        return None
