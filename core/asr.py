from __future__ import annotations

import glob
import os
import re
import site
from dataclasses import dataclass
from pathlib import Path

import config

_SENTENCE_END = ("。", "！", "？", "…", "!", "?", ".")


@dataclass
class Cue:
    start: float
    end: float
    text: str


_model = None
_device_used = ""
_load_note = ""


def _register_cuda_dlls() -> None:
    """Windows 下把 torch / nvidia 包自带的 CUDA DLL 目录加进搜索路径。"""
    if os.name != "nt":
        return
    roots = []
    try:
        roots.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        roots.append(site.getusersitepackages())
    except Exception:
        pass

    candidates = []
    for root in roots:
        candidates.extend(glob.glob(os.path.join(root, "nvidia", "*", "bin")))
        candidates.extend(glob.glob(os.path.join(root, "torch", "lib")))
        candidates.extend(glob.glob(os.path.join(root, "ctranslate2", "*.dll")))
    for path in candidates:
        try:
            if os.path.isdir(path):
                os.add_dll_directory(path)
                os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
        except OSError:
            continue


def get_model():
    """加载（并缓存）Whisper 模型；CUDA 失败时自动退回 CPU。"""
    global _model, _device_used, _load_note
    if _model is not None:
        return _model

    from faster_whisper import WhisperModel

    model_path = config.WHISPER_MODEL
    if not Path(model_path, "model.bin").exists():
        raise RuntimeError(
            f"未找到 Whisper 模型：{model_path}\n"
            "请先执行：python scripts/download_models.py --whisper"
        )

    _register_cuda_dlls()
    want_cuda = config.WHISPER_DEVICE in ("auto", "cuda")
    if want_cuda:
        try:
            _model = WhisperModel(model_path, device="cuda", compute_type=config.WHISPER_COMPUTE)
            _device_used = "cuda"
            _load_note = f"Whisper 使用 GPU（{config.WHISPER_COMPUTE}）"
            return _model
        except Exception as exc:
            if config.WHISPER_DEVICE == "cuda":
                raise RuntimeError(f"Whisper GPU 加载失败：{exc}") from exc
            _load_note = f"Whisper GPU 不可用，已退回 CPU：{exc}"

    _model = WhisperModel(model_path, device="cpu", compute_type=config.WHISPER_COMPUTE_CPU)
    _device_used = "cpu"
    if not _load_note:
        _load_note = "Whisper 使用 CPU（会明显变慢）"
    return _model


def device_note() -> str:
    """识别设备的可读说明；模型还没加载时给出发起前的预估。"""
    if _load_note:
        return _load_note
    return f"尚未加载（首次识别时自动加载，预计 {predict_device()}）"


def predict_device() -> str:
    """不加载模型，仅探测 CUDA 是否可用，用于界面显示。"""
    if config.WHISPER_DEVICE == "cpu":
        return f"CPU / {config.WHISPER_COMPUTE_CPU}"
    try:
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
        supported = ctranslate2.get_supported_compute_types("cuda")
    except Exception:
        return f"CPU / {config.WHISPER_COMPUTE_CPU}（无法检测显卡）"

    if count > 0 and config.WHISPER_COMPUTE in supported:
        return f"GPU / {config.WHISPER_COMPUTE}"
    if count > 0:
        return f"GPU / {config.WHISPER_COMPUTE}（显卡可能不支持该精度，加载失败会自动退回 CPU）"
    if config.WHISPER_DEVICE == "cuda":
        return f"GPU / {config.WHISPER_COMPUTE}（未检测到可用显卡，加载会失败）"
    return f"CPU / {config.WHISPER_COMPUTE_CPU}（未检测到可用显卡）"


def transcribe(
    audio_path: str | Path,
    language: str | None,
    progress=None,
) -> tuple[list[Cue], dict]:
    """识别音频，返回带时间戳的断句列表。"""
    model = get_model()

    segments, info = model.transcribe(
        str(audio_path),
        language=language,
        task="transcribe",  # translate 只能转英文，中文必须靠后面的 LLM
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        word_timestamps=True,
        condition_on_previous_text=False,  # 避免长视频里的复读循环
    )

    total = getattr(info, "duration", 0) or 0
    cues: list[Cue] = []
    buffer: list[tuple[float, float, str]] = []

    def flush() -> None:
        if not buffer:
            return
        text = "".join(part for _, _, part in buffer).strip()
        if text:
            cues.append(Cue(start=buffer[0][0], end=buffer[-1][1], text=text))
        buffer.clear()

    for seg in segments:
        words = getattr(seg, "words", None)
        if words:
            for word in words:
                buffer.append((word.start, word.end, word.word))
                joined = "".join(part for _, _, part in buffer).strip()
                duration = buffer[-1][1] - buffer[0][0]
                if (
                    len(joined) >= config.MAX_CUE_CHARS
                    or duration >= config.MAX_CUE_SECONDS
                    or joined.endswith(_SENTENCE_END)
                ):
                    flush()
        else:
            flush()
            cues.extend(_split_segment(seg))

        if progress and total:
            progress(min(seg.end / total, 0.99), desc=f"语音识别 {seg.end / total:.0%}")

    flush()
    cues = _merge_short(cues)
    meta = {
        "language": getattr(info, "language", "") or "",
        "language_probability": float(getattr(info, "language_probability", 0) or 0),
        "duration": float(total),
    }
    return cues, meta


def _split_segment(seg) -> list[Cue]:
    """没有词级时间戳时，按标点切分，并按字数比例分配时间。"""
    text = (seg.text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?；;])", text) if p.strip()]
    total_chars = sum(len(p) for p in parts) or 1
    start = float(seg.start)
    span = float(seg.end) - start
    out: list[Cue] = []
    for part in parts:
        piece = span * len(part) / total_chars
        out.append(Cue(start=start, end=start + piece, text=part))
        start += piece
    return out


def _merge_short(cues: list[Cue]) -> list[Cue]:
    """把过短的句子合并到前一条，避免字幕闪烁。"""
    merged: list[Cue] = []
    for cue in cues:
        if (
            merged
            and merged[-1].end - merged[-1].start < config.MIN_CUE_SECONDS
            and len(merged[-1].text) + len(cue.text) <= config.MAX_CUE_CHARS
        ):
            prev = merged[-1]
            merged[-1] = Cue(prev.start, cue.end, _join_text(prev.text, cue.text))
        else:
            merged.append(cue)
    return merged


def _join_text(left: str, right: str) -> str:
    if left and right and left[-1].isascii() and right[0].isascii():
        return f"{left} {right}"
    return left + right
