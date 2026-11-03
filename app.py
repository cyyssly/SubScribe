from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

import gradio as gr
from gradio.utils import get_upload_folder

import config
from core.asr import Cue
from core.export import (
    BURN_TARGETS,
    ExportCancelled,
    burn_subtitles,
    default_burn_format,
    mux_soft_subtitles,
    soft_target,
)
from core.pipeline import process_video
from core.subtitles import build_srt, parse_subtitles
from core.translator import Translator


def _video_path(value) -> str | None:
    if not value:
        return None
    if isinstance(value, dict):
        return value.get("video") or value.get("path") or value.get("name")
    return str(value)


def _format_duration(seconds: float) -> str:
    total = int(seconds or 0)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _status_text(ok: bool, message: str) -> str:
    whisper_ready = config.whisper_model_ready()
    gguf_ready = config.LLAMA_MODEL.exists()
    server_ready = config.LLAMA_SERVER.exists()
    mark = lambda ready: "就绪" if ready else "缺失"  # noqa: E731
    return (
        f"- 翻译服务（{config.LLAMA_BASE_URL}）：{message}\n"
        f"- Whisper 模型：{mark(whisper_ready)}（`{Path(config.WHISPER_MODEL).name}`）\n"
        f"- 翻译模型：{mark(gguf_ready)}（`{config.LLAMA_MODEL.name}`）\n"
        f"- llama-server：{mark(server_ready)}\n"
        f"- 识别设备：{_asr_note()}"
    )


def backend_status() -> str:
    ok, message = Translator().health()
    return _status_text(ok, message)


def poll_status():
    """每秒轮询一次；翻译服务就绪后关闭轮询。"""
    ok, message = Translator().health()
    return _status_text(ok, message), gr.Timer(active=not ok)


def _asr_note() -> str:
    from core.asr import device_note

    return device_note()


# ---------- 字幕文件路径 ----------


def _default_output_path(video) -> str:
    source = _video_path(video)
    stem = Path(source).stem if source else "字幕"
    return str(config.OUTPUT_DIR / f"{stem}.zh.srt")


def _resolve_output_path(text: str, video) -> Path:
    raw = (text or "").strip().strip('"')
    if not raw:
        raw = _default_output_path(video)
    path = Path(raw).expanduser()
    if path.suffix.lower() != ".srt":
        path = path.with_suffix(".srt")
    if not path.is_absolute():
        path = config.BASE_DIR / path
    return path


# ---------- 字幕数据 ----------

# 表格每次改动都会把最新数据同步回来，保存时直接使用这份数据。
_rows_json = "[]"


def sync_rows(rows_json: str) -> None:
    global _rows_json
    _rows_json = rows_json


def _entries_from_json(text: str) -> list[dict]:
    try:
        data = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"表格数据无法解析：{exc}") from exc
    if not isinstance(data, list):
        raise ValueError("表格数据格式不正确")

    entries: list[dict] = []
    for item in data:
        start = max(0.0, float(item.get("start") or 0))
        end = max(start, float(item.get("end") or start))
        entries.append({"start": start, "end": end, "text": str(item.get("text") or "")})
    return entries


def _json_from_entries(entries: list[dict]) -> str:
    payload = [
        {
            "start": round(float(e["start"]), 3),
            "end": round(float(e["end"]), 3),
            "text": str(e["text"]),
        }
        for e in entries
    ]
    # 数据会嵌进 HTML 的 textarea，避开可能提前闭合的 </
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


def _srt_text(entries: list[dict]) -> str:
    cues = [Cue(float(e["start"]), float(e["end"]), str(e["text"])) for e in entries]
    return build_srt(cues, [[str(e["text"])] for e in entries])


# ---------- 视频挂载字幕 ----------


def _vtt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def _write_vtt(entries: list[dict]) -> Path:
    """生成浏览器可直接播放的 WebVTT 字幕（Gradio 的 SRT 转换会写坏时间行）。"""
    target = Path(get_upload_folder()) / f"subs_{uuid.uuid4().hex}.vtt"
    lines = ["WEBVTT", ""]
    for entry in entries:
        lines.append(f"{_vtt_timestamp(entry['start'])} --> {_vtt_timestamp(entry['end'])}")
        lines.append(str(entry["text"]).replace("\r\n", "\n"))
        lines.append("")
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def _fresh_video_link(source: str) -> str:
    """指向同一份视频的新链接，用于让播放器重新挂载并从头播放。"""
    suffix = Path(source).suffix or ".mp4"
    target = Path(get_upload_folder()) / f"subs_{uuid.uuid4().hex}{suffix}"
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
    return str(target)


def _video_with_subtitles(video_source: str | None, entries: list[dict]):
    update = {"subtitles": str(_write_vtt(entries)), "playback_position": 0}
    if video_source:
        update["value"] = _fresh_video_link(video_source)
    return gr.update(**update)


# ---------- 字幕表格组件 ----------

TABLE_TEMPLATE = """
<div class="subs-shell">
  <div class="subs-toolbar">
    <div class="subs-count">字幕预览 · 共 <span class="subs-n">0</span> 条</div>
    <div class="subs-offset">
      <span>整体偏移</span>
      <input class="subs-offset-input" type="number" step="0.1" value="0">
      <span>秒</span>
      <button class="subs-btn subs-offset-apply">应用</button>
    </div>
    <div class="subs-history">
      <button class="subs-btn subs-undo" disabled>撤销</button>
      <button class="subs-btn subs-redo" disabled>重做</button>
    </div>
  </div>
  <div class="subs-scroll">
    <table class="subs-table">
      <thead>
        <tr>
          <th class="col-idx">序号</th>
          <th class="col-time">开始</th>
          <th class="col-time">结束</th>
          <th>字幕</th>
        </tr>
      </thead>
      <tbody class="subs-body"></tbody>
    </table>
  </div>
  <textarea class="subs-data" aria-hidden="true">${value}</textarea>
</div>
"""

TABLE_CSS = """
.subs-shell { display: flex; flex-direction: column; gap: 8px; }
.subs-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; font-size: 13px; }
.subs-count { color: var(--body-text-color-subdued, #666); }
.subs-offset { display: flex; align-items: center; gap: 6px; }
.subs-offset input { width: 64px; padding: 3px 6px; border: 1px solid var(--border-color-primary, #ddd); border-radius: 4px; background: var(--background-fill-primary, #fff); color: var(--body-text-color, #111); }
.subs-btn { padding: 3px 12px; border-radius: 4px; border: 1px solid var(--border-color-primary, #ddd); background: var(--background-fill-secondary, #f5f5f5); color: var(--body-text-color, #111); cursor: pointer; font-size: 13px; }
.subs-btn:disabled { opacity: 0.4; cursor: default; }
.subs-history { display: flex; gap: 6px; }
.subs-scroll { max-height: 520px; overflow: auto; border: 1px solid var(--border-color-primary, #ddd); border-radius: 6px; }
.subs-table { width: 100%; border-collapse: collapse; font-size: 13px; table-layout: fixed; }
.subs-table th { position: sticky; top: 0; z-index: 1; background: var(--background-fill-secondary, #f7f7f7); padding: 6px 8px; text-align: left; font-weight: 600; border-bottom: 1px solid var(--border-color-primary, #ddd); }
.subs-table td { padding: 4px 8px; border-bottom: 1px solid var(--border-color-primary, #ececec); vertical-align: top; }
.col-idx { width: 96px; }
.col-time { width: 188px; }
.tr-sel, .subs-table tr.sel { background: rgba(255, 152, 0, 0.10); }
.cell-idx { white-space: nowrap; }
.num { color: var(--body-text-color-subdued, #888); }
.icons { display: none; margin-left: 4px; }
.cell.sel .icons { display: inline-flex; gap: 2px; vertical-align: middle; }
.ibtn { display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px; padding: 0; border: none; border-radius: 4px; background: transparent; color: var(--body-text-color-subdued, #666); cursor: pointer; }
.ibtn:hover { background: rgba(127, 127, 127, 0.15); color: var(--color-accent, #f97316); }
.ic { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
.tinput { width: 104px; padding: 2px 4px; border: 1px solid transparent; border-radius: 3px; background: transparent; color: var(--body-text-color, #111); font-family: ui-monospace, Consolas, monospace; font-size: 12.5px; }
.tinput:hover, .tinput:focus { border-color: var(--border-color-primary, #ccc); background: var(--background-fill-primary, #fff); outline: none; }
.tarea { width: 100%; min-height: 34px; padding: 2px 4px; border: 1px solid transparent; border-radius: 3px; background: transparent; color: var(--body-text-color, #111); resize: vertical; font-family: inherit; font-size: 13px; line-height: 1.45; }
.tarea:hover, .tarea:focus { border-color: var(--border-color-primary, #ccc); background: var(--background-fill-primary, #fff); outline: none; }
.subs-empty { padding: 20px; text-align: center; color: var(--body-text-color-subdued, #888); }
.subs-data { display: none; }
"""

TABLE_JS = """
const state = { rows: [], history: [], future: [], selected: null, editSnapshot: null };
let syncTimer = null;

function q(sel) { return element.querySelector(sel); }
function pad2(n) { return String(n).padStart(2, '0'); }
function round3(v) { return Math.round(v * 1000) / 1000; }

function fmt(sec) {
  const ms = Math.max(0, Math.round((Number(sec) || 0) * 1000));
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  return pad2(h) + ':' + pad2(m) + ':' + pad2(s) + ',' + String(ms % 1000).padStart(3, '0');
}

function parseTime(text) {
  const raw = String(text == null ? '' : text).trim().replace(/，/g, ',');
  if (!raw) return null;
  const parts = raw.split(':');
  if (parts.length > 3) return null;
  let total = 0;
  for (const part of parts) {
    const n = Number(String(part).replace(',', '.'));
    if (!isFinite(n)) return null;
    total = total * 60 + n;
  }
  return total;
}

function esc(text) {
  return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

const ICON_PLUS = '<svg class="ic" viewBox="0 0 16 16"><path d="M8 3.6v8.8M3.6 8h8.8"/></svg>';
const ICON_TRASH = '<svg class="ic" viewBox="0 0 16 16"><path d="M3.2 5h9.6"/><path d="M6.2 5V3.4h3.6V5"/><path d="M4.6 5l.6 8.2h5.6l.6-8.2"/><path d="M6.9 7.6v3.4M9.1 7.6v3.4"/></svg>';
const ICON_LEFT = '<svg class="ic" viewBox="0 0 16 16"><path d="M9.8 3.6 5.4 8l4.4 4.4"/></svg>';
const ICON_RIGHT = '<svg class="ic" viewBox="0 0 16 16"><path d="M6.2 3.6 10.6 8l-4.4 4.4"/></svg>';

function timeCell(col, value) {
  return '<td class="cell cell-time" data-col="' + col + '">'
    + '<input class="tinput" value="' + fmt(value) + '" spellcheck="false">'
    + '<span class="icons"><button class="ibtn minus" title="提前 0.1 秒">' + ICON_LEFT + '</button>'
    + '<button class="ibtn plus" title="延后 0.1 秒">' + ICON_RIGHT + '</button></span></td>';
}

function idxCell(i) {
  return '<td class="cell cell-idx" data-col="idx"><span class="num">' + (i + 1) + '</span>'
    + '<span class="icons"><button class="ibtn add" title="在下方新增一行">' + ICON_PLUS + '</button>'
    + '<button class="ibtn del" title="删除本行">' + ICON_TRASH + '</button></span></td>';
}

function textCell(text) {
  return '<td class="cell cell-text" data-col="text">'
    + '<textarea class="tarea" rows="2" spellcheck="false">' + esc(text) + '</textarea></td>';
}

function rowHtml(row, i) {
  return '<tr data-row="' + i + '">' + idxCell(i) + timeCell('start', row.start)
    + timeCell('end', row.end) + textCell(row.text) + '</tr>';
}

function render() {
  const body = q('.subs-body');
  if (!body) return;
  const scroll = q('.subs-scroll');
  const top = scroll ? scroll.scrollTop : 0;
  if (!state.rows.length) {
    body.innerHTML = '<tr><td colspan="4" class="subs-empty">还没有字幕，生成后会自动显示。</td></tr>';
  } else {
    body.innerHTML = state.rows.map(rowHtml).join('');
  }
  if (scroll) scroll.scrollTop = top;
  const counter = q('.subs-n');
  if (counter) counter.textContent = String(state.rows.length);
  applySelection();
  updateHistory();
}

function applySelection() {
  const body = q('.subs-body');
  if (!body) return;
  body.querySelectorAll('tr').forEach(function (tr) { tr.classList.remove('sel'); });
  body.querySelectorAll('td.cell').forEach(function (td) { td.classList.remove('sel'); });
  if (!state.selected) return;
  const tr = body.querySelector('tr[data-row="' + state.selected.row + '"]');
  if (!tr) return;
  tr.classList.add('sel');
  const td = tr.querySelector('td[data-col="' + state.selected.col + '"]');
  if (td) td.classList.add('sel');
}

function updateHistory() {
  const undo = q('.subs-undo');
  const redo = q('.subs-redo');
  if (undo) undo.disabled = !state.history.length;
  if (redo) redo.disabled = !state.future.length;
}

function pushHistory() {
  state.history.push(JSON.stringify(state.rows));
  if (state.history.length > 100) state.history.shift();
  state.future.length = 0;
  updateHistory();
}

function syncNow() {
  if (syncTimer) { clearTimeout(syncTimer); syncTimer = null; }
  try {
    if (server && server.sync_rows) server.sync_rows(JSON.stringify(state.rows));
  } catch (err) {
    console.error('同步字幕数据失败', err);
  }
}

function syncLater() {
  if (syncTimer) clearTimeout(syncTimer);
  syncTimer = setTimeout(syncNow, 200);
}

function setTime(i, col, value) {
  const row = state.rows[i];
  if (!row) return;
  const v = Math.max(0, round3(value));
  if (col === 'start') {
    row.start = v;
    if (row.end < row.start) row.end = row.start;
  } else {
    row.end = Math.max(row.start, v);
  }
}

function refreshRowTimes(i) {
  const tr = q('tr[data-row="' + i + '"]');
  const row = state.rows[i];
  if (!tr || !row) return;
  const startInput = tr.querySelector('td[data-col="start"] input');
  const endInput = tr.querySelector('td[data-col="end"] input');
  if (startInput) startInput.value = fmt(row.start);
  if (endInput) endInput.value = fmt(row.end);
}

function commitTime(input) {
  const td = input.closest('td');
  const tr = input.closest('tr');
  if (!td || !tr) return;
  const i = Number(tr.dataset.row);
  const col = td.dataset.col;
  const row = state.rows[i];
  if (!row) return;
  const value = parseTime(input.value);
  if (value === null || !isFinite(value)) {
    input.value = fmt(row[col]);
    return;
  }
  if (Math.abs(value - row[col]) < 0.0005) {
    input.value = fmt(row[col]);
    return;
  }
  pushHistory();
  setTime(i, col, value);
  refreshRowTimes(i);
  syncNow();
}

function nudge(i, col, delta) {
  const row = state.rows[i];
  if (!row) return;
  pushHistory();
  setTime(i, col, row[col] + delta);
  state.selected = { row: i, col: col };
  refreshRowTimes(i);
  applySelection();
  syncNow();
}

function addRow(i) {
  const row = state.rows[i];
  if (!row) return;
  pushHistory();
  const next = state.rows[i + 1];
  const start = round3(row.end);
  const end = next ? Math.max(start, round3(next.start)) : round3(start + 2);
  state.rows.splice(i + 1, 0, { start: start, end: end, text: '手动新增字幕' });
  state.selected = { row: i + 1, col: 'idx' };
  render();
  syncNow();
}

function deleteRow(i) {
  if (!state.rows[i]) return;
  pushHistory();
  state.rows.splice(i, 1);
  if (state.selected && state.selected.row >= i) state.selected = null;
  render();
  syncNow();
}

function undoAction() {
  if (!state.history.length) return;
  state.future.push(JSON.stringify(state.rows));
  state.rows = JSON.parse(state.history.pop());
  state.selected = null;
  render();
  syncNow();
  updateHistory();
}

function redoAction() {
  if (!state.future.length) return;
  state.history.push(JSON.stringify(state.rows));
  state.rows = JSON.parse(state.future.pop());
  state.selected = null;
  render();
  syncNow();
  updateHistory();
}

function applyOffset() {
  const input = q('.subs-offset-input');
  if (!input || !state.rows.length) return;
  const delta = Number(input.value);
  if (!isFinite(delta) || delta === 0) return;
  pushHistory();
  state.rows.forEach(function (row) {
    row.start = Math.max(0, round3(row.start + delta));
    row.end = Math.max(row.start, round3(row.end + delta));
  });
  input.value = '0';
  render();
  syncNow();
}

element.addEventListener('click', function (ev) {
  const target = ev.target;
  if (!target || !target.closest) return;
  const undoBtn = target.closest('.subs-undo');
  const redoBtn = target.closest('.subs-redo');
  const applyBtn = target.closest('.subs-offset-apply');
  const newBtn = target.closest('.ibtn-add-empty');
  const iconBtn = target.closest('.ibtn');
  const td = target.closest('td.cell');

  if (undoBtn) { undoAction(); return; }
  if (redoBtn) { redoAction(); return; }
  if (applyBtn) { applyOffset(); return; }
  if (newBtn) {
    pushHistory();
    state.rows.push({ start: 0, end: 2, text: '手动新增字幕' });
    state.selected = { row: state.rows.length - 1, col: 'idx' };
    render();
    syncNow();
    return;
  }
  if (!td) return;

  const tr = td.closest('tr');
  if (!tr) return;
  const i = Number(tr.dataset.row);
  const col = td.dataset.col;

  if (iconBtn) {
    ev.preventDefault();
    state.selected = { row: i, col: col };
    if (iconBtn.classList.contains('add')) addRow(i);
    else if (iconBtn.classList.contains('del')) deleteRow(i);
    else if (iconBtn.classList.contains('minus')) nudge(i, col, -0.1);
    else if (iconBtn.classList.contains('plus')) nudge(i, col, 0.1);
    return;
  }

  state.selected = { row: i, col: col };
  applySelection();
  if (col === 'text') {
    const ta = td.querySelector('textarea');
    if (ta) ta.focus();
  } else if (col !== 'idx') {
    const input = td.querySelector('input');
    if (input) { input.focus(); input.select(); }
  }
});

element.addEventListener('change', function (ev) {
  const target = ev.target;
  if (target && target.classList && target.classList.contains('tinput')) commitTime(target);
});

element.addEventListener('input', function (ev) {
  const target = ev.target;
  if (!target || !target.classList || !target.classList.contains('tarea')) return;
  const tr = target.closest('tr');
  if (!tr) return;
  const i = Number(tr.dataset.row);
  if (!state.rows[i]) return;
  state.rows[i].text = target.value;
  syncLater();
});

element.addEventListener('focusin', function (ev) {
  const target = ev.target;
  if (!target || !target.classList || !target.classList.contains('tarea')) return;
  const tr = target.closest('tr');
  if (!tr) return;
  const i = Number(tr.dataset.row);
  if (!state.rows[i]) return;
  state.editSnapshot = { row: i, text: state.rows[i].text, json: JSON.stringify(state.rows) };
});

element.addEventListener('focusout', function (ev) {
  const target = ev.target;
  if (!target || !target.classList || !target.classList.contains('tarea')) return;
  const snapshot = state.editSnapshot;
  state.editSnapshot = null;
  if (!snapshot) return;
  const tr = target.closest('tr');
  const i = tr ? Number(tr.dataset.row) : -1;
  const row = state.rows[i];
  if (!row || snapshot.row !== i || row.text === snapshot.text) {
    syncNow();
    return;
  }
  state.history.push(snapshot.json);
  if (state.history.length > 100) state.history.shift();
  state.future.length = 0;
  updateHistory();
  syncNow();
});

function readData() {
  const holder = q('.subs-data');
  return holder ? holder.value : '[]';
}

function load(raw) {
  let data = [];
  try {
    const parsed = JSON.parse(raw || '[]');
    if (Array.isArray(parsed)) data = parsed;
  } catch (err) {
    data = [];
  }
  state.rows = data.map(function (item) {
    const start = Math.max(0, Number(item.start) || 0);
    const end = Math.max(start, Number(item.end) || start);
    return { start: round3(start), end: round3(end), text: String(item.text == null ? '' : item.text) };
  });
  state.history = [];
  state.future = [];
  state.selected = null;
  state.editSnapshot = null;
  render();
  syncNow();
}

load(readData());
watch('value', function () { load(readData()); });
"""


# ---------- 取消生成 ----------

# Gradio 的 cancels 只能取消排队中的事件，正在执行的函数不会被中断，
# 所以这里用协作式取消：进度回调里检查标志，命中就抛异常让流水线退出。
_cancel_flag = threading.Event()


class GenerationCancelled(Exception):
    pass


def request_cancel():
    _cancel_flag.set()
    return "已请求取消，当前批次结束后停止..."


def _cancellable(progress):
    """把取消标志接入进度回调，让流水线在每个检查点自行中断。"""

    def report(value, desc=None, **kwargs):
        if _cancel_flag.is_set():
            raise GenerationCancelled()
        if desc is None:
            progress(value, **kwargs)
        else:
            progress(value, desc=desc, **kwargs)

    return report


# ---------- 事件处理 ----------


def run_pipeline(
    video,
    video_source,
    language_label,
    subtitle_label,
    target_text,
    srt_path_value,
    progress=gr.Progress(),
):
    global _rows_json
    _cancel_flag.clear()
    path = _video_path(video_source) or _video_path(video)
    if not path:
        return "请先上传一个视频文件。", srt_path_value, gr.update(), gr.update(), ""
    if not target_text:
        return "已取消生成。", srt_path_value, gr.update(), gr.update(), ""

    try:
        out_path = _resolve_output_path(target_text, video)
    except Exception as exc:
        return f"保存路径无效：{exc}", srt_path_value, gr.update(), gr.update(), ""

    try:
        result = process_video(
            path,
            language=config.LANGUAGES.get(language_label),
            subtitle_languages=config.SUBTITLE_LANGUAGES.get(subtitle_label, ("zh-Hans",)),
            out_path=out_path,
            progress=_cancellable(progress),
        )
    except GenerationCancelled:
        return "", srt_path_value, gr.update(), gr.update(), ""
    except Exception as exc:  # 直接把可读的错误抛给界面
        return f"生成失败：{exc}", srt_path_value, gr.update(), gr.update(), ""

    entries = [
        {"start": cue.start, "end": cue.end, "text": "\n".join(lines)}
        for cue, lines in zip(result.cues, result.lines)
    ]
    _rows_json = _json_from_entries(entries)

    summary = (
        f"完成：共 {result.cue_count} 条字幕，视频时长 {_format_duration(result.duration)}，"
        f"识别语种 {result.language or '未知'}（{result.asr_device}），"
        f"字幕语言 {subtitle_label}\n\n"
        f"字幕已保存到 {result.srt_path}"
    )
    if not result.translated:
        summary += "\n\n本次无需翻译，直接使用识别原文。"

    return (
        summary,
        str(result.srt_path),
        _rows_json,
        _video_with_subtitles(_video_path(video) or path, entries),
        path,
    )


def save_subtitles(srt_path_value, video_source, video):
    try:
        entries = _entries_from_json(_rows_json)
    except ValueError as exc:
        return f"保存失败：{exc}", gr.update()
    if not entries:
        return "保存失败：没有可保存的字幕。", gr.update()

    try:
        path = _resolve_output_path(srt_path_value, video_source or video)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_srt_text(entries), encoding="utf-8")
    except Exception as exc:
        return f"保存失败：{exc}", gr.update()

    source = _video_path(video_source) or _video_path(video)
    return f"已保存到：{path}", _video_with_subtitles(source, entries)


# ---------- 导出带字幕的视频 ----------

ZH_FONTS = [
    ("微软雅黑", "Microsoft YaHei"),
    ("黑体", "SimHei"),
    ("宋体", "SimSun"),
    ("楷体", "KaiTi"),
    ("等线", "DengXian"),
    ("思源黑体", "Noto Sans SC"),
]

EN_FONTS = [
    ("Arial", "Arial"),
    ("Segoe UI", "Segoe UI"),
    ("Times New Roman", "Times New Roman"),
    ("Calibri", "Calibri"),
    ("Verdana", "Verdana"),
    ("Impact", "Impact"),
]


def _export_entries() -> list[dict]:
    entries = _entries_from_json(_rows_json)
    if not entries:
        raise ValueError("没有可用的字幕，请先生成字幕或手动添加。")
    return entries


def _burn_format_default(video) -> str:
    path = _video_path(video)
    if not path:
        return "mp4"
    try:
        return default_burn_format(path)
    except Exception:
        return "mp4"


# ---------- 上传后的在线预览 ----------


def _wait_until_readable(path: str, timeout: float = 45.0) -> bool:
    """刚上传的大文件常被系统扫描临时占用，等能正常读取再交给浏览器。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with open(path, "rb"):
                return True
        except PermissionError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.3)
        except OSError:
            return False


def _fresh_preview_link(source: str) -> str | None:
    """给上传文件换一个新链接，让播放器在文件可读后重新加载。"""
    target = Path(get_upload_folder()) / f"preview_{uuid.uuid4().hex}.mp4"
    try:
        os.link(source, target)
        return str(target)
    except OSError:
        pass
    try:
        shutil.copy2(source, target)
        return str(target)
    except OSError:
        return None


def _playable(source: str) -> bool:
    try:
        from gradio.processing_utils import video_is_playable

        return video_is_playable(source)
    except Exception:
        return True


def _patch_file_response_retry() -> None:
    """Windows 上刚上传的大文件可能被系统扫描短暂占用，读取会得到 PermissionError，
    导致播放请求返回 500、前端误报“Video not playable”。这里给 Gradio 与 Starlette
    的文件响应加一小段重试，等占用释放后正常返回。"""
    targets = []
    try:
        from gradio import ranged_response

        targets.append(ranged_response.RangedFileResponse)
    except Exception:
        pass
    try:
        from starlette.responses import FileResponse

        targets.append(FileResponse)
    except Exception:
        pass

    for response_cls in targets:
        original = response_cls.__call__
        if getattr(original, "_retrying_permission", False):
            continue

        async def retrying_call(self, scope, receive, send, _original=original):
            # Starlette 的 FileResponse 会先发送响应头再打开文件，
            # 因此需要先等文件可读，避免头部已发出后读取失败。
            path = getattr(self, "path", None)
            if path:
                try:
                    if os.path.exists(path):
                        await asyncio.to_thread(_wait_until_readable, path, 45.0)
                except Exception:
                    pass

            deadline = time.monotonic() + 45.0
            while True:
                started = False

                async def tracking_send(message):
                    nonlocal started
                    started = True
                    await send(message)

                try:
                    await _original(self, scope, receive, tracking_send)
                    return
                except PermissionError:
                    if started or time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.25)

        retrying_call._retrying_permission = True  # type: ignore[attr-defined]
        response_cls.__call__ = retrying_call  # type: ignore[method-assign]


_patch_file_response_retry()


def prepare_video(video):
    """上传视频后：填默认字幕/格式，并保证预览能在浏览器里播放。"""
    source = _video_path(video)
    if source and Path(source).name.startswith(("preview_", "subs_")):
        # 事件把预览链接写回组件时可能再次触发 input，直接跳过，避免反复重建链接
        return (gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip())

    preview = source
    note = ""

    if source:
        readable = _wait_until_readable(source)
        try:
            if not _playable(source):
                from gradio.processing_utils import convert_video_to_playable_mp4

                converted = convert_video_to_playable_mp4(source)
                if converted and str(converted) != source:
                    preview = str(converted)
                    note = "原视频编码浏览器无法直接播放，已生成预览副本；生成字幕仍使用原视频。"
            if preview == source:
                fresh = _fresh_preview_link(source)
                if fresh:
                    preview = fresh
        except Exception:
            preview = source
        if not readable and preview == source:
            note = "视频文件暂时被其他程序占用，若无法预览请稍后重新上传。"

    video_update = (
        gr.update(value=preview) if preview and preview != source else gr.update()
    )
    return (
        _default_output_path(video),
        gr.update(value=_burn_format_default(source or video)),
        video_update,
        source or "",
        note or gr.update(),
    )


# ---------- 本地设置（记住上次选择的导出目录） ----------

SETTINGS_FILE = config.BASE_DIR / "settings.json"


def _load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_setting(key: str, value) -> None:
    data = _load_settings()
    data[key] = value
    try:
        SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def pick_export_path(
    initial_dir: str,
    default_name: str,
    ext: str,
    kind: str = "视频",
    title: str = "选择导出位置",
) -> Path | None:
    """弹出「另存为」对话框，可同时指定目录和文件名；取消返回 None。"""
    stored = str(_load_settings().get("export_dir") or "")
    start = stored if stored and Path(stored).exists() else initial_dir
    if not Path(start).exists():
        start = str(config.OUTPUT_DIR)
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.asksaveasfilename(
            parent=root,
            initialdir=start,
            initialfile=default_name,
            defaultextension=ext,
            filetypes=[(f"{ext.lstrip('.').upper()} {kind}", f"*{ext}"), ("所有文件", "*.*")],
            title=title,
        )
        root.destroy()
    except Exception as exc:
        raise RuntimeError(f"无法打开保存对话框：{exc}") from exc
    if not chosen:
        return None
    target = Path(chosen)
    _save_setting("export_dir", str(target.parent))
    return target


def choose_run_target(video, srt_path_value):
    """点击生成前先选保存目录和文件名，确定后才开始生成。"""
    if not _video_path(video):
        return "", "请先上传一个视频文件。"
    try:
        default = _resolve_output_path(srt_path_value, video)
        target = pick_export_path(
            str(default.parent),
            default.name,
            ".srt",
            kind="字幕",
            title="选择字幕保存位置",
        )
    except Exception as exc:
        return "", f"无法选择保存路径：{exc}"
    if target is None:
        return "", "已取消生成。"
    return str(target), ""


def load_subtitles(file):
    """把现成的字幕文件读进表格，方便直接把本工具当烧录工具用。"""
    global _rows_json
    raw = file if isinstance(file, str) else getattr(file, "name", "") or ""
    path = Path(raw)
    if not raw or not path.exists():
        return "请选择一个字幕文件。", gr.update(), gr.update()

    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="gbk")
        except Exception as exc:
            return f"读取字幕失败：{exc}", gr.update(), gr.update()

    cues = parse_subtitles(text, path.suffix)
    if not cues:
        return f"没有从 {path.name} 解析到字幕条目。", gr.update(), gr.update()

    entries = [{"start": cue.start, "end": cue.end, "text": cue.text} for cue in cues]
    rows = _json_from_entries(entries)
    _rows_json = rows
    saved_path = str(config.OUTPUT_DIR / f"{path.stem}.srt")
    return (
        f"已加载 {len(entries)} 条字幕：{path.name}\n\n"
        "接下来上传视频即可软挂载或硬烧录。",
        rows,
        saved_path,
    )


def choose_soft_target(srt_path_value, video_source, video):
    """先弹「另存为」选路径，选好后再由 soft_mount 执行导出。"""
    source = _video_path(video_source) or _video_path(video)
    if not source:
        return "", "请先上传视频。"
    try:
        _export_entries()
        srt = _resolve_output_path(srt_path_value, source)
        _, ext = soft_target(source)
        target = pick_export_path(str(srt.parent), f"{srt.stem}{ext}", ext)
    except Exception as exc:
        return "", f"软挂载失败：{exc}"
    if target is None:
        return "", "已取消导出。"
    return str(target), ""


def soft_mount(srt_path_value, target_text, video_source, video, progress=gr.Progress()):
    """把字幕作为独立字幕轨封装进视频，画面不重新编码。"""
    _cancel_flag.clear()
    source = _video_path(video_source) or _video_path(video)
    if not source:
        return "请先上传视频。"
    if not target_text:
        return "已取消导出。"

    try:
        entries = _export_entries()
        srt = _resolve_output_path(srt_path_value, source)
        srt.parent.mkdir(parents=True, exist_ok=True)
        srt.write_text(_srt_text(entries), encoding="utf-8")
        out = mux_soft_subtitles(source, srt, Path(target_text), progress=progress)
    except Exception as exc:
        return f"软挂载失败：{exc}"

    return f"已导出软字幕视频：{out}\n\n字幕文件同步更新：{srt}"


def choose_burn_target(out_format, srt_path_value, video_source, video):
    """先弹「另存为」选路径，选好后再由 burn_video 执行烧录。"""
    source = _video_path(video_source) or _video_path(video)
    if not source:
        return "", "请先上传视频。"
    try:
        _export_entries()
        srt = _resolve_output_path(srt_path_value, source)
        ext = "." + (out_format if out_format in BURN_TARGETS else "mp4")
        target = pick_export_path(str(srt.parent), f"{srt.stem}.hard{ext}", ext)
    except Exception as exc:
        return "", f"烧录失败：{exc}"
    if target is None:
        return "", "已取消导出。"
    return str(target), ""


def burn_video(
    zh_font,
    zh_size,
    en_font,
    en_size,
    out_format,
    srt_path_value,
    target_text,
    video_source,
    video,
    progress=gr.Progress(),
):
    """把字幕画进画面，输出单一视频文件（重新编码，耗时较长）。"""
    _cancel_flag.clear()
    source = _video_path(video_source) or _video_path(video)
    if not source:
        return "请先上传视频。"
    if not target_text:
        return "已取消导出。"

    try:
        entries = _export_entries()
        out = burn_subtitles(
            source,
            entries,
            Path(target_text),
            zh_font=zh_font,
            zh_size=int(zh_size),
            en_font=en_font,
            en_size=int(en_size),
            container=out_format,
            progress=progress,
            should_cancel=_cancel_flag.is_set,
        )
    except ExportCancelled:
        return ""
    except Exception as exc:
        return f"烧录失败：{exc}"

    return f"已导出烧录视频：{out}"


def build_ui() -> gr.Blocks:
    config.ensure_dirs()

    with gr.Blocks(title="视频字幕生成器") as demo:
        gr.Markdown(
            "# 视频字幕生成器\n"
            "上传视频，本地完成语音识别 + 中文翻译；生成后可直接在页面预览、微调时间轴并导出 SRT。"
        )

        with gr.Row():
            with gr.Column(scale=3):
                video = gr.Video(label="视频", sources=["upload"], height=340, autoplay=True)
                with gr.Row():
                    language = gr.Dropdown(
                        choices=list(config.LANGUAGES.keys()),
                        value="自动识别",
                        label="源语言",
                    )
                    subtitle_language = gr.Dropdown(
                        choices=list(config.SUBTITLE_LANGUAGES.keys()),
                        value="简体中文",
                        label="字幕语言",
                    )
                with gr.Row():
                    run_button = gr.Button("开始生成字幕", variant="primary")
                    cancel_button = gr.Button("取消生成", variant="stop")
                # 用文本框承载进度条：Markdown 组件不显示 Gradio 的进度动画。
                progress_box = gr.Textbox(
                    label="生成进度",
                    show_label=False,
                    interactive=False,
                    lines=2,
                    max_lines=8,
                    placeholder="点击“开始生成字幕”后，这里会显示生成进度与结果。",
                )
                gr.Markdown("### 服务状态")
                status_md = gr.Markdown(backend_status())
                refresh_button = gr.Button("刷新服务状态")

            with gr.Column(scale=4):
                with gr.Row():
                    load_button = gr.UploadButton(
                        "加载字幕",
                        file_types=[".srt", ".vtt", ".ass"],
                        scale=0,
                    )
                table = gr.HTML(
                    value="[]",
                    html_template=TABLE_TEMPLATE,
                    css_template=TABLE_CSS,
                    js_on_load=TABLE_JS,
                    apply_default_css=False,
                    server_functions=[sync_rows],
                    container=False,
                    padding=False,
                )
                # 文本框兼作进度条载体，Markdown 组件不会显示进度动画。
                edit_msg = gr.Textbox(
                    label="字幕编辑状态",
                    show_label=False,
                    interactive=False,
                    lines=2,
                    max_lines=6,
                    placeholder="保存、加载或软挂载的结果会显示在这里。",
                )
                with gr.Row():
                    save_button = gr.Button("保存独立字幕文件", variant="secondary")
                    soft_button = gr.Button("软挂载字幕到视频", variant="secondary")
                with gr.Accordion("硬烧录字幕到视频（重新编码，耗时较长）", open=True):
                    with gr.Row():
                        zh_font = gr.Dropdown(
                            choices=ZH_FONTS, value="Microsoft YaHei", label="中文字体"
                        )
                        zh_size = gr.Slider(
                            minimum=24, maximum=80, value=44, step=2,
                            label="中文字号（按 1080p 计）",
                        )
                    with gr.Row():
                        en_font = gr.Dropdown(
                            choices=EN_FONTS, value="Arial", label="英文字体"
                        )
                        en_size = gr.Slider(
                            minimum=16, maximum=56, value=30, step=2,
                            label="英文字号（按 1080p 计）",
                        )
                    with gr.Row():
                        burn_format = gr.Dropdown(
                            choices=["mp4", "mkv", "mov"],
                            value="mp4",
                            label="输出格式",
                            info="上传视频后自动切到原视频格式；原格式不支持时用 mp4。",
                        )
                        burn_button = gr.Button("硬烧录字幕到视频", variant="primary")
                    burn_msg = gr.Textbox(
                        label="烧录进度",
                        show_label=False,
                        interactive=False,
                        lines=2,
                        max_lines=6,
                        placeholder="硬烧录的进度与结果会显示在这里。",
                    )
                video_source = gr.State("")
                srt_path_state = gr.State("")
                srt_target = gr.State("")
                soft_target = gr.State("")
                burn_target = gr.State("")
                poll_timer = gr.Timer(1)

        video.input(
            prepare_video,
            inputs=[video],
            outputs=[srt_path_state, burn_format, video, video_source, progress_box],
        )
        run_button.click(
            choose_run_target,
            inputs=[video, srt_path_state],
            outputs=[srt_target, progress_box],
            show_progress="hidden",
        ).then(
            run_pipeline,
            inputs=[
                video, video_source, language, subtitle_language,
                srt_target, srt_path_state,
            ],
            outputs=[progress_box, srt_path_state, table, video, video_source],
            show_progress_on=[progress_box],
        ).then(backend_status, outputs=status_md, show_progress="hidden")
        cancel_button.click(
            request_cancel,
            outputs=[progress_box],
            show_progress="hidden",
            queue=False,
        )
        save_button.click(
            save_subtitles,
            inputs=[srt_path_state, video_source, video],
            outputs=[edit_msg, video],
            show_progress="hidden",
        )
        load_button.upload(
            load_subtitles,
            inputs=[load_button],
            outputs=[edit_msg, table, srt_path_state],
            show_progress="hidden",
        )
        soft_button.click(
            choose_soft_target,
            inputs=[srt_path_state, video_source, video],
            outputs=[soft_target, edit_msg],
            show_progress="hidden",
        ).then(
            soft_mount,
            inputs=[srt_path_state, soft_target, video_source, video],
            outputs=[edit_msg],
            show_progress_on=[edit_msg],
        )
        burn_button.click(
            choose_burn_target,
            inputs=[burn_format, srt_path_state, video_source, video],
            outputs=[burn_target, burn_msg],
            show_progress="hidden",
        ).then(
            burn_video,
            inputs=[
                zh_font, zh_size, en_font, en_size, burn_format,
                srt_path_state, burn_target, video_source, video,
            ],
            outputs=[burn_msg],
            show_progress_on=[burn_msg],
        )
        refresh_button.click(backend_status, outputs=status_md)
        demo.load(poll_status, outputs=[status_md, poll_timer])
        poll_timer.tick(poll_status, outputs=[status_md, poll_timer])

    return demo


# 视频已显示时 Gradio 不再渲染上传区，拖入文件会触发浏览器默认的「打开文件」行为，
# 这里在 document 上拦截文件拖放，避免意外弹出新窗口。
DROP_GUARD_HTML = """
<script>
(function () {
  var isFileDrag = function (event) {
    var data = event.dataTransfer;
    if (!data) return false;
    if (data.files && data.files.length) return true;
    var types = data.types || [];
    for (var i = 0; i < types.length; i++) {
      if (types[i] === "Files") return true;
    }
    return false;
  };
  var guard = function (event) {
    if (isFileDrag(event)) event.preventDefault();
  };
  document.addEventListener("dragover", guard, true);
  document.addEventListener("drop", guard, true);
})();
</script>
"""


if __name__ == "__main__":
    build_ui().launch(
        server_name="127.0.0.1",
        server_port=7860,
        inbrowser=True,
        show_error=True,
        head=DROP_GUARD_HTML,
    )
