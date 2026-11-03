from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class FFmpegNotFound(RuntimeError):
    pass


def find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise FFmpegNotFound(
            "未找到 ffmpeg。请安装 ffmpeg 并加入 PATH，或执行 pip install imageio-ffmpeg。"
        )


def extract_audio(video_path: str | Path, work_dir: str | Path) -> Path:
    """把视频容器里的音频抽成 16kHz 单声道 wav（Whisper 的标准输入）。"""
    src = Path(video_path)
    if not src.exists():
        raise FileNotFoundError(f"视频文件不存在：{src}")

    out = Path(work_dir) / f"{src.stem}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        find_ffmpeg(),
        "-y",
        "-i", str(src),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(out),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore",
        creationflags=creationflags,
    )
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg 抽音频失败：{(proc.stderr or '').strip()[-800:]}")

    if out.stat().st_size < 1024:
        raise RuntimeError("音频轨道为空，视频可能没有声音。")
    return out
