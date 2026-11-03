from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

import config
from core.audio import find_ffmpeg

# 容器 -> (字幕编码, 扩展名)；只有这几种容器能干净地挂软字幕
SOFT_TARGETS = {
    "mp4": ("mov_text", ".mp4"),
    "mov": ("mov_text", ".mov"),
    "m4v": ("mov_text", ".m4v"),
    "mkv": ("srt", ".mkv"),
    "webm": ("webvtt", ".webm"),
}

# 硬烧录本质上就是重新编码，只保留常见容器
BURN_TARGETS = ("mp4", "mkv", "mov")

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class ExportError(RuntimeError):
    pass


class ExportCancelled(Exception):
    pass


def _creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def find_ffprobe() -> str:
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    guess = Path(find_ffmpeg()).with_name("ffprobe.exe")
    if guess.exists():
        return str(guess)
    raise ExportError("未找到 ffprobe，请确认 ffmpeg 的 bin 目录已加入 PATH。")


def probe_video(path: str | Path) -> dict:
    cmd = [
        find_ffprobe(),
        "-v", "error",
        "-show_entries", "format=format_name,duration:stream=codec_type,width,height",
        "-of", "json",
        str(path),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="ignore", creationflags=_creationflags(),
    )
    if proc.returncode != 0:
        raise ExportError(f"读取视频信息失败：{(proc.stderr or '').strip()[-600:]}")
    data = json.loads(proc.stdout or "{}")
    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), {}
    )
    fmt = data.get("format", {})
    return {
        "format_name": str(fmt.get("format_name") or ""),
        "duration": float(fmt.get("duration") or 0),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
    }


def _container(info: dict, path: str | Path) -> str:
    names = {n.strip() for n in info.get("format_name", "").split(",")}
    suffix = Path(path).suffix.lower().lstrip(".")
    # matroska 与 webm 共用同一个 demuxer（format_name 为 matroska,webm），所以先看后缀
    if suffix == "webm":
        return "webm"
    if suffix == "mkv":
        return "mkv"
    if "matroska" in names:
        return "mkv"
    if "webm" in names:
        return "webm"
    if suffix in {"mp4", "mov", "m4v"}:
        return suffix
    if "mp4" in names or "mov" in names:
        return "mp4"
    return suffix or "mp4"


def soft_target(video: str | Path) -> tuple[str, str]:
    """返回 (字幕编码, 扩展名)；容器不支持挂软字幕时退回 MP4。"""
    container = _container(probe_video(video), video)
    return SOFT_TARGETS.get(container, SOFT_TARGETS["mp4"])


def default_burn_format(video: str | Path) -> str:
    """硬烧录的默认输出格式：能用原格式就用原格式，否则 MP4。"""
    try:
        container = _container(probe_video(video), video)
    except ExportError:
        return "mp4"
    return container if container in BURN_TARGETS else "mp4"


def _report(progress, value: float, desc: str) -> None:
    if progress is None:
        return
    try:
        progress(min(max(value, 0.0), 1.0), desc=desc)
    except TypeError:
        progress(min(max(value, 0.0), 1.0))


def mux_soft_subtitles(
    video: str | Path,
    srt_path: str | Path,
    out_path: str | Path,
    progress=None,
) -> Path:
    """把 SRT 作为字幕轨塞进视频容器，视频/音频流原样拷贝。"""
    source = Path(video)
    srt = Path(srt_path)
    if not source.exists():
        raise ExportError(f"视频不存在：{source}")
    if not srt.exists():
        raise ExportError(f"字幕文件不存在：{srt}")

    _report(progress, 0.05, "读取视频信息")
    codec, ext = soft_target(source)
    out = Path(out_path)
    if out.suffix.lower() != ext:
        out = out.with_suffix(ext)
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        find_ffmpeg(), "-y",
        "-i", str(source),
        "-i", str(srt),
        "-map", "0:v:0", "-map", "0:a?", "-map", "1:0",
        "-c:v", "copy", "-c:a", "copy",
        "-c:s", codec,
        "-metadata:s:s:0", "language=chi",
        "-disposition:s:0", "default",
    ]
    if ext in {".mp4", ".mov", ".m4v"}:
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(out))

    _report(progress, 0.2, "挂载字幕")
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="ignore", creationflags=_creationflags(),
    )
    if proc.returncode != 0 or not out.exists():
        detail = (proc.stderr or "").strip()[-800:]
        hint = ""
        if "Permission denied" in detail or "PermissionError" in detail:
            hint = (
                "\n\n提示：目标文件夹不允许写入。可能是文件被占用、"
                "或该目录受 Windows「受控文件夹访问」/只读属性限制，请换一个目录再试。"
            )
        raise ExportError("挂载软字幕失败（原视频可能是不支持的容器或编码）。\n" + detail + hint)
    _report(progress, 1.0, "完成")
    return out


# ---------- 硬烧录 ----------


def _ass_timestamp(seconds: float) -> str:
    total_cs = max(0, int(round(seconds * 100)))
    hours, rem = divmod(total_cs, 360_000)
    minutes, rem = divmod(rem, 6_000)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_text(text: str) -> str:
    clean = str(text).replace("\r\n", "\n").replace("\r", "\n")
    clean = clean.replace("{", "（").replace("}", "）").replace("\\", "＼")
    return clean.replace("\n", r"\N")


def _line_is_cjk(line: str) -> bool:
    return bool(_CJK_RE.search(line))


def build_ass(
    entries: Sequence[dict],
    zh_font: str,
    zh_size: int,
    en_font: str,
    en_size: int,
    width: int,
    height: int,
) -> str:
    """按 1080p 基准排版，libass 会自动缩放到实际分辨率。"""
    play_y = 1080
    play_x = int(round(play_y * width / height)) if width and height else 1920
    zh_outline = round(zh_size / 18, 1)
    en_outline = round(en_size / 18, 1)
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_x}",
        f"PlayResY: {play_y}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: ZH,{zh_font},{zh_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        f"0,0,0,0,100,100,0,0,1,{zh_outline},1,2,40,40,48,1",
        f"Style: EN,{en_font},{en_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        f"0,0,0,0,100,100,0,0,1,{en_outline},1,2,40,40,48,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    body: list[str] = []
    for entry in entries:
        lines = [ln for ln in str(entry["text"]).split("\n") if ln.strip()]
        if not lines:
            continue
        chunks = [
            "{\\r" + ("ZH" if _line_is_cjk(line) else "EN") + "}" + _ass_text(line)
            for line in lines
        ]
        body.append(
            f"Dialogue: 0,{_ass_timestamp(entry['start'])},"
            f"{_ass_timestamp(entry['end'])},ZH,,0,0,0,,{'\\N'.join(chunks)}"
        )
    return "\n".join(header + body) + "\n"


_NVENC_CHECKED = False
_NVENC_OK = False


def _has_nvenc() -> bool:
    global _NVENC_CHECKED, _NVENC_OK
    if _NVENC_CHECKED:
        return _NVENC_OK
    _NVENC_CHECKED = True
    try:
        out = subprocess.run(
            [find_ffmpeg(), "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            creationflags=_creationflags(),
        ).stdout
        _NVENC_OK = "h264_nvenc" in out
    except Exception:
        _NVENC_OK = False
    return _NVENC_OK


def _video_encode_args() -> list[str]:
    if _has_nvenc():
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "20"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]


def burn_subtitles(
    video: str | Path,
    entries: Sequence[dict],
    out_path: str | Path,
    zh_font: str = "Microsoft YaHei",
    zh_size: int = 44,
    en_font: str = "Arial",
    en_size: int = 30,
    container: str | None = None,
    progress=None,
    should_cancel=None,
) -> Path:
    """把字幕画进画面，输出单一视频文件。"""
    source = Path(video)
    if not source.exists():
        raise ExportError(f"视频不存在：{source}")
    if not entries:
        raise ExportError("没有可烧录的字幕。")

    _report(progress, 0.03, "读取视频信息")
    info = probe_video(source)
    source_container = _container(info, source)
    target = (container or source_container).lower()
    ext = "." + (target if target in BURN_TARGETS else "mp4")

    out = Path(out_path).with_suffix(ext)
    out.parent.mkdir(parents=True, exist_ok=True)

    work_dir = config.OUTPUT_DIR / "_work" / "burn"
    work_dir.mkdir(parents=True, exist_ok=True)
    ass_path = work_dir / "burn.ass"
    ass_path.write_text(
        build_ass(entries, zh_font, int(zh_size), en_font, int(en_size),
                  info["width"], info["height"]),
        encoding="utf-8",
    )

    needs_aac = ext in {".mp4", ".mov"} and source_container not in {"mp4", "mov", "m4v"}
    cmd = [
        find_ffmpeg(), "-y",
        "-i", str(source),
        "-vf", f"subtitles={ass_path.name}",
        "-pix_fmt", "yuv420p",
        *_video_encode_args(),
    ]
    cmd += (["-c:a", "aac", "-b:a", "192k"] if needs_aac else ["-c:a", "copy"])
    if ext in {".mp4", ".mov"}:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", "-loglevel", "error", str(out)]

    _report(progress, 0.08, "开始烧录")
    duration = float(info.get("duration") or 0)
    proc = subprocess.Popen(
        cmd, cwd=str(work_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="ignore", creationflags=_creationflags(),
    )
    try:
        for line in proc.stdout:
            if should_cancel and should_cancel():
                proc.kill()
                raise ExportCancelled()
            if line.startswith("out_time_us=") and duration > 0:
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                _report(progress, 0.08 + min(done / duration, 1.0) * 0.9, "烧录字幕")
        stderr = (proc.stderr or "").read() if proc.stderr else ""
    finally:
        proc.stdout.close()
        proc.stderr.close()
        proc.wait()

    if proc.returncode != 0 or not out.exists():
        detail = (stderr or "").strip()[-800:]
        hint = ""
        if "Permission denied" in detail or "PermissionError" in detail:
            hint = (
                "\n\n提示：目标文件夹不允许写入。可能是文件被占用、"
                "或该目录受 Windows「受控文件夹访问」/只读属性限制，请换一个目录再试。"
            )
        raise ExportError("烧录字幕失败。\n" + detail + hint)
    _report(progress, 1.0, "完成")
    return out
