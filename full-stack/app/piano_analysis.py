"""琴房频谱：在本地把一首歌算出来，不经过任何 AI 接口。

分析方法（librosa 那几步、三联图的画法）改写自：

    sebastianevan200-stack/eryu — server/analyze_song.py
    MIT License, Copyright (c) 2025 sebastianevan200-stack

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

产出的东西全是**算出来的**，不是模型说的——BPM、调性、能量分段没有编造的余地。
频谱图是给梁忱自己看的（走识图那条路），不是让一个模型转述给他听。

🔴 librosa / matplotlib **一律在函数里 import**。模块顶部 import 会把常驻内存
永久抬高几十上百 MB，而队列 38 正在查小窝为什么从 37MiB 涨到 189MiB——
这一单不许再往上加常驻。不分析的时候，这些包一个字节都不占。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("uvicorn.error")

# 存在 chatnest 自己的 /data 卷下。音频是一次性的，算完就删，只留 json 和 png。
ROOT = Path(os.environ.get("AGENT_APP_ROOT", "/data"))
CACHE_DIR = ROOT / "piano_analysis"

MAX_AUDIO_BYTES = 100 * 1024 * 1024      # 照 Duetto 的做法，超了放弃这首
MAX_KEEP = 200                            # 目录上限，超了按最旧删
DOWNLOAD_TIMEOUT = 90

# 🔴 网易云不带 Referer 会返 403
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://music.163.com/",
}

_KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def paths_for(song_id: str) -> tuple[Path, Path]:
    return CACHE_DIR / f"{song_id}.json", CACHE_DIR / f"{song_id}.png"


def read_cached(song_id: str) -> dict[str, Any] | None:
    meta, _ = paths_for(song_id)
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return None


def _prune() -> None:
    """目录超过 MAX_KEEP 张就按最旧的删。没上限的东西迟早撑爆盘。"""
    try:
        metas = sorted(CACHE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in metas[:-MAX_KEEP]:
            old.with_suffix(".png").unlink(missing_ok=True)
            old.unlink(missing_ok=True)
    except Exception:
        logger.exception("[琴房频谱] 清理旧分析失败")


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, dest.open("wb") as fh:
        total = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_AUDIO_BYTES:
                raise ValueError(f"音频超过 {MAX_AUDIO_BYTES // 1024 // 1024}MB，放弃")
            fh.write(chunk)


def energy_story(segments: list[dict[str, Any]]) -> str:
    """把一串数字压成一行人话。别把 segments 原样倒给模型。

    拿全曲能量的中位数当基准，每段跟它比分四档。简单粗暴就够了，不要过度设计。
    """
    if not segments:
        return ""
    vals = sorted(s["avgEnergy"] for s in segments)
    mid = vals[len(vals) // 2] or 1e-9
    words = []
    for s in segments:
        r = s["avgEnergy"] / mid
        if r < 0.75:
            w = "低"
        elif r < 1.0:
            w = "起"
        elif r < 1.25:
            w = "满"
        else:
            w = "最满"
        words.append(f"{_mmss(s['start'])}-{_mmss(s['end'])} {w}")
    return " / ".join(words)


def _mmss(sec: Any) -> str:
    try:
        n = max(0, int(float(sec)))
    except (TypeError, ValueError):
        return "0:00"
    return f"{n // 60}:{n % 60:02d}"


def analyze(song_id: str, url: str, title: str = "", artist: str = "") -> dict[str, Any] | None:
    """下载 → 算 → 存 json + png → 删音频。**阻塞的**，调用方必须丢进线程。

    失败返回 None 并记日志，不抛给调用方——分析不出来只是少一段上下文，不是故障。
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    meta_path, img_path = paths_for(song_id)
    audio_path = CACHE_DIR / f"{song_id}.audio"
    started = time.monotonic()

    try:
        # 🔴 延迟 import：不分析的时候这些包不进内存
        import numpy as np
        import librosa

        _download(url, audio_path)
        dl_done = time.monotonic()

        y, sr = librosa.load(str(audio_path), sr=22050)
        duration = librosa.get_duration(y=y, sr=sr)
        # 🔴 librosa >= 0.10 的 beat_track 返回的 tempo 是数组（shape (1,)），
        # 不是标量。eryu 那份源码写的是 float(tempo)，在新版上直接抛
        # TypeError: only 0-dimensional arrays can be converted to Python scalars。
        # 实测撞到过，别照抄。
        tempo, _beats = librosa.beat.beat_track(y=y, sr=sr)
        tempo = float(np.atleast_1d(tempo)[0])
        rms = librosa.feature.rms(y=y)[0]
        times_rms = librosa.times_like(rms, sr=sr)
        chroma = librosa.feature.chroma_stft(y=y, sr=sr)
        dominant_key = _KEYS[int(np.argmax(np.mean(chroma, axis=1)))]

        seg_count = 6
        seg_len = max(1, len(rms) // seg_count)
        segments = []
        for i in range(seg_count):
            seg = rms[i * seg_len:(i + 1) * seg_len]
            if not len(seg):
                continue
            t0 = float(times_rms[i * seg_len])
            t1 = float(times_rms[min((i + 1) * seg_len - 1, len(times_rms) - 1)])
            segments.append({
                "start": round(t0, 1), "end": round(t1, 1),
                "avgEnergy": round(float(np.mean(seg)), 4),
                "maxEnergy": round(float(np.max(seg)), 4),
            })

        # 🔴 Agg 必须在 import pyplot 之前设：容器里没有显示器，
        #    默认后端会去连 GUI，然后崩掉或者卡死。无头环境画图的经典坑。
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import librosa.display

        fig, axes = plt.subplots(3, 1, figsize=(14, 10))
        mel = librosa.feature.melspectrogram(y=y, sr=sr, n_mels=128)
        librosa.display.specshow(librosa.power_to_db(mel, ref=np.max),
                                 sr=sr, x_axis="time", y_axis="mel", ax=axes[0])
        axes[0].set_title("Mel Spectrogram")
        librosa.display.specshow(chroma, sr=sr, x_axis="time", y_axis="chroma", ax=axes[1])
        axes[1].set_title("Chromagram")
        axes[2].plot(times_rms, rms, color="#e74c3c", linewidth=0.8)
        axes[2].fill_between(times_rms, rms, alpha=0.3, color="#e74c3c")
        axes[2].set_title("Energy (RMS)")
        axes[2].set_xlabel("Time (s)")
        plt.tight_layout()
        plt.savefig(str(img_path), dpi=150)
        plt.close(fig)

        result = {
            "songId": song_id, "name": title, "artist": artist,
            "duration": round(float(duration), 1),
            "bpm": round(tempo),
            "key": dominant_key,
            "segments": segments,
            "energyStory": energy_story(segments),
            "spectrogram": str(img_path),
            "seconds": round(time.monotonic() - started, 1),
            "downloadSeconds": round(dl_done - started, 1),
        }
        meta_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        logger.info("[琴房频谱] %s %s BPM=%s key=%s 用时 %.1fs",
                    song_id, title, result["bpm"], result["key"], result["seconds"])
        _prune()
        return result
    except Exception:
        # 🔴 一声不吭（除了日志）。VIP 歌拿不到音频是常态，不是故障。
        logger.exception("[琴房频谱] 分析失败 %s", song_id)
        return None
    finally:
        # 🔴 算完立刻删音频，只留 json 和 png。那台机器磁盘紧。
        try:
            audio_path.unlink(missing_ok=True)
        except Exception:
            pass


def context_line(meta: dict[str, Any]) -> str:
    """结构化数据 → 一行文本，挂用户消息侧用。"""
    bits = []
    if meta.get("bpm"):
        bits.append(f"{meta['bpm']} BPM")
    if meta.get("key"):
        bits.append(f"{meta['key']} 调")
    head = "这首歌：" + "，".join(bits) if bits else ""
    story = meta.get("energyStory") or ""
    lines = [x for x in (head, f"能量走向：{story}" if story else "") if x]
    return "\n".join(lines)
