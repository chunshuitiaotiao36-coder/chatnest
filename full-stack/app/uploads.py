import mimetypes
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, UploadFile


ROOT = Path(os.environ.get("AGENT_APP_ROOT", Path(__file__).resolve().parent.parent)).expanduser().resolve()
UPLOAD_ROOT = ROOT / "uploads"
MAX_FILE_BYTES = 60 * 1024 * 1024
MAX_FILES = 10
ALLOWED_EXTENSIONS = {
    ".bmp",
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".csv",
    ".gif",
    ".go",
    ".h",
    ".heic",
    ".heif",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".pdf",
    ".php",
    ".png",
    ".ps1",
    ".py",
    ".rb",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".text",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".webp",
    ".xml",
    ".yaml",
    ".yml",
}
IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".webp",
}


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _safe_name(filename: str) -> str:
    original = Path(filename or "file").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", original).strip("._")
    return cleaned[:160] or "file"


def attachment_metadata(path: Path) -> dict:
    mime, _ = mimetypes.guess_type(path.name)
    return {
        "name": path.name.split("_", 2)[-1],
        "path": str(path.resolve()),
        "mime": mime or "application/octet-stream",
        "size": path.stat().st_size,
        "is_image": path.suffix.lower() in IMAGE_EXTENSIONS,
    }


async def save_uploads(conv_id: str, files: list[UploadFile]) -> list[dict]:
    if not files or len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail="请选择 1 到 10 个文件")
    target_dir = (UPLOAD_ROOT / conv_id).resolve()
    if not _inside(target_dir, UPLOAD_ROOT):
        raise HTTPException(status_code=400, detail="无效会话路径")
    target_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict] = []
    created_paths: list[Path] = []
    target: Path | None = None
    try:
        for upload in files:
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in ALLOWED_EXTENSIONS:
                raise HTTPException(
                    status_code=415,
                    detail=f"不支持的文件类型：{upload.filename}",
                )
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            filename = (
                f"{timestamp}_{uuid.uuid4().hex[:8]}_"
                f"{_safe_name(upload.filename)}"
            )
            target = (target_dir / filename).resolve()
            if not _inside(target, target_dir):
                raise HTTPException(status_code=400, detail="无效文件名")
            size = 0
            try:
                with target.open("wb") as output:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise HTTPException(
                                status_code=413,
                                detail=f"{upload.filename} 超过 60MB",
                            )
                        output.write(chunk)
                created_paths.append(target)
                saved.append(attachment_metadata(target))
            finally:
                await upload.close()
    except Exception:
        for path in created_paths:
            path.unlink(missing_ok=True)
        if target is not None:
            target.unlink(missing_ok=True)
        raise
    return saved


def validated_attachments(
    conv_id: str,
    paths: list[str],
) -> list[dict]:
    if len(paths) > MAX_FILES:
        raise HTTPException(status_code=400, detail="附件数量过多")
    conversation_root = (UPLOAD_ROOT / conv_id).resolve()
    result = []
    for raw_path in paths:
        path = Path(raw_path).resolve()
        if not _inside(path, conversation_root) or not path.is_file():
            raise HTTPException(status_code=400, detail="附件路径无效")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail="附件超过 60MB")
        result.append(attachment_metadata(path))
    return result


def validated_file(conv_id: str, filename: str) -> Path:
    path = (UPLOAD_ROOT / conv_id / Path(filename).name).resolve()
    if not _inside(path, UPLOAD_ROOT / conv_id) or not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return path


def remove_conversation_uploads(conv_id: str) -> None:
    target = (UPLOAD_ROOT / conv_id).resolve()
    if _inside(target, UPLOAD_ROOT) and target.is_dir():
        shutil.rmtree(target)
