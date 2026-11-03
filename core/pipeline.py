from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import config
from core.asr import Cue, device_note, transcribe
from core.audio import extract_audio
from core.subtitles import build_srt, write_srt
from core.translator import Translator


@dataclass
class SubtitleResult:
    srt_path: Path
    srt_text: str
    cue_count: int
    language: str
    duration: float
    asr_device: str
    translated: bool
    languages: list[str]
    cues: list[Cue]
    lines: list[list[str]]


# 判断整段识别结果是否已经是简体中文：命中任意一个繁体专用字即视为繁体。
_CHINESE_LANGUAGES = {"zh", "yue"}
_TRADITIONAL_CHARS = frozenset(
    "這們說時會來對麼為開關學國見覺種樣過還點電話車門問間聽讓給現實應該誰發幾"
    "氣無東頭買賣錢讀寫認識講語詞論議記張雙離難雖隨陽陰隊陳陸與樂歡舊嗎後優壞"
    "錯號圖請牆熱節網課題級統經濟華灣區縣鄉醫藥廳廚飯麵湯魚雞豬鳥馬龍愛夢憶懷"
    "慶驚嚇煩願邊塊場處勝戰爭軍楊劉謝吳鄭羅蘇葉誇務員費總結決準確認顯檢測試驗"
    "頻訊個體職興舉麗億歲歷據條機權觀錄訪談響鐘"
)


def is_simplified_chinese(text: str) -> bool:
    return not any(ch in _TRADITIONAL_CHARS for ch in text)


def _report(progress, value: float, desc: str) -> None:
    if progress is None:
        return
    try:
        progress(min(max(value, 0.0), 1.0), desc=desc)
    except TypeError:
        progress(min(max(value, 0.0), 1.0))


def process_video(
    video_path: str | Path,
    language: str | None,
    subtitle_languages: Sequence[str] | None = None,
    out_path: str | Path | None = None,
    progress=None,
) -> SubtitleResult:
    config.ensure_dirs()
    source = Path(video_path)
    if not source.exists():
        raise FileNotFoundError(f"视频不存在：{source}")

    safe_stem = re.sub(r'[\\/:*?"<>|]', "_", source.stem) or "video"
    work_dir = config.OUTPUT_DIR / "_work" / safe_stem
    work_dir.mkdir(parents=True, exist_ok=True)

    _report(progress, 0.02, "抽取音频")
    audio_path = extract_audio(source, work_dir)

    _report(progress, 0.05, "加载语音模型")
    cues, meta = transcribe(
        audio_path,
        language=language,
        progress=lambda value, desc=None: _report(progress, 0.05 + value * 0.55, desc or "语音识别"),
    )
    if not cues:
        raise RuntimeError("没有识别到任何语音，请确认视频包含人声。")

    _report(progress, 0.62, "准备翻译")
    languages = list(subtitle_languages or ("zh-Hans",))
    source_lines = [cue.text.strip() for cue in cues]
    effective_lang = (language or meta.get("language") or "").strip().lower()
    transcript = "".join(source_lines)
    source_is_simplified_zh = (
        effective_lang in _CHINESE_LANGUAGES and is_simplified_chinese(transcript)
    )

    # source 直接用识别原文；目标语言与源语言一致时也不必再翻
    texts: dict[str, list[str]] = {"source": source_lines}
    pending: list[str] = []
    for target in languages:
        if target == "source" or target in texts:
            continue
        if target == "zh-Hans" and source_is_simplified_zh:
            texts[target] = source_lines
        elif target == "en" and effective_lang == "en":
            texts[target] = source_lines
        else:
            pending.append(target)

    translated = bool(pending)
    if pending:
        translator = Translator()
        ok, message = translator.health()
        if not ok:
            raise RuntimeError(f"{message}\n请先启动翻译服务：scripts\\start_llama.ps1")
        span = 0.33 / len(pending)
        for position, target in enumerate(pending):
            base = 0.62 + span * position
            texts[target] = translator.translate(
                cues,
                target=target,
                progress=lambda value, desc=None, start=base: _report(
                    progress, start + value * span, desc or "翻译"
                ),
            )
    else:
        _report(progress, 0.95, "无需翻译，直接使用识别原文")

    lines = [[texts[lang][index] for lang in languages] for index in range(len(cues))]

    _report(progress, 0.97, "生成字幕文件")
    srt_path = (
        Path(out_path).expanduser()
        if out_path
        else config.OUTPUT_DIR / f"{safe_stem}.zh.srt"
    )
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    write_srt(srt_path, cues, lines)
    srt_text = build_srt(cues, lines)

    _report(progress, 1.0, "完成")
    return SubtitleResult(
        srt_path=srt_path,
        srt_text=srt_text,
        cue_count=len(cues),
        language=meta.get("language", ""),
        duration=meta.get("duration", 0.0),
        asr_device=device_note(),
        translated=translated,
        languages=languages,
        cues=list(cues),
        lines=lines,
    )
