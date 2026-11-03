from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from core.asr import Cue

_TS = r"(?:(\d{1,3}):)?(\d{1,3}):(\d{1,2})[,.](\d{1,3})"
_CUE_RE = re.compile(rf"{_TS}\s*-->\s*{_TS}")
_ASS_RE = re.compile(r"^Dialogue:\s*(.*)$", re.I)
_ASS_TAG_RE = re.compile(r"\{[^}]*\}")


def format_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _ts_to_seconds(groups: tuple, offset: int) -> float:
    hours = int(groups[offset] or 0)
    minutes = int(groups[offset + 1])
    secs = int(groups[offset + 2])
    frac = (groups[offset + 3] or "").ljust(3, "0")[:3]
    return hours * 3600 + minutes * 60 + secs + int(frac) / 1000


def _ass_to_seconds(value: str) -> float | None:
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, secs = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(secs.replace(",", "."))
    except ValueError:
        return None


def _parse_cue_blocks(body: str) -> list[Cue]:
    """解析 SRT / WebVTT 的「时间行 + 若干文本行」结构。"""
    lines = body.split("\n")
    cues: list[Cue] = []
    index = 0
    while index < len(lines):
        match = _CUE_RE.search(lines[index])
        if not match:
            index += 1
            continue
        start = _ts_to_seconds(match.groups(), 0)
        end = _ts_to_seconds(match.groups(), 4)
        index += 1
        block: list[str] = []
        while index < len(lines) and lines[index].strip():
            block.append(lines[index].strip())
            index += 1
        text = "\n".join(block)
        if text:
            cues.append(Cue(start, max(start, end), text))
    return cues


def _parse_ass(body: str) -> list[Cue]:
    """只取 Dialogue 行，去掉样式标签与换行标记。"""
    cues: list[Cue] = []
    for line in body.split("\n"):
        match = _ASS_RE.match(line.strip())
        if not match:
            continue
        fields = match.group(1).split(",", 9)
        if len(fields) < 10:
            continue
        start = _ass_to_seconds(fields[1])
        end = _ass_to_seconds(fields[2])
        if start is None or end is None:
            continue
        raw = _ASS_TAG_RE.sub("", fields[9])
        text = raw.replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ").strip()
        if text:
            cues.append(Cue(start, max(start, end), text))
    return cues


def parse_subtitles(text: str, suffix: str = "") -> list[Cue]:
    body = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if suffix.lower() == ".ass" or "[Events]" in body or _ASS_RE.search(body):
        return _parse_ass(body)
    return _parse_cue_blocks(body)


def build_srt(
    cues: Sequence[Cue],
    lines: Sequence[Sequence[str]],
) -> str:
    blocks: list[str] = []
    for index, (cue, cue_lines) in enumerate(zip(cues, lines), start=1):
        text = "\n".join(line.strip() for line in cue_lines if line.strip())
        blocks.append(
            f"{index}\n"
            f"{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n"
            f"{text}\n"
        )
    return "\n".join(blocks).rstrip() + "\n"


def write_srt(
    path: str | Path,
    cues: Sequence[Cue],
    lines: Sequence[Sequence[str]],
) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_srt(cues, lines), encoding="utf-8")
    return out
