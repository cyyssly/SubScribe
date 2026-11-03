from __future__ import annotations

import re
from typing import Sequence

import httpx

import config
from core.asr import Cue

_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.S | re.I)
_NUMBERED_RE = re.compile(r"^\s*(?:-+\s*)?(\d{1,3})\s*[|｜:：.、)]\s*(.+?)\s*$")


class TranslatorError(RuntimeError):
    pass


SYSTEM_PROMPT = """你是一名专业的影视字幕翻译。把输入的字幕逐条翻译成{target}，供观众直接阅读。

必须遵守：
1. 只输出译文，不要输出原文、解释、注释、拼音或任何额外说明。
2. 严格逐条对应：编号保持不变，顺序不变，不许合并或拆分条目。
3. 输出格式只能是「编号|译文」，一条一行，例如：3|{example}
4. 每条译文尽量不超过 {max_chars} 个{unit}；空间不够时先精简措辞，但不要丢掉关键信息。
5. 保持上下文连贯：代词、称谓、人名、专有名词要和前文一致。
6. 口语按口语译，不要过度书面化，不要添加原文没有的信息。"""

SINGLE_PROMPT = """你是一名专业的影视字幕翻译。把下面这条字幕翻译成{target}。

只输出译文，不要解释、不要加引号、不要保留原文。译文尽量不超过 {max_chars} 个{unit}，口语化，自然通顺。"""

# 目标语言 -> (提示词里的语言名, 计量单位, 单条上限, 示例)
TARGET_SPECS = {
    "zh-Hans": ("简体中文", "汉字", config.MAX_SUBTITLE_CHARS, "我们走吧。"),
    "zh-Hant": (
        "繁体中文（使用港台常用的繁体字，绝对不要出现简体字）",
        "汉字",
        config.MAX_SUBTITLE_CHARS,
        "我們走吧。",
    ),
    "en": ("英文", "英文字符", config.MAX_LATIN_CHARS, "Let's go."),
}


class Translator:
    def __init__(self, base_url: str | None = None, model_name: str | None = None):
        self.base_url = (base_url or config.LLAMA_BASE_URL).rstrip("/")
        self.model_name = model_name or config.LLAMA_MODEL_NAME
        headers = {}
        if config.LLAMA_API_KEY and config.LLAMA_API_KEY != "not-needed":
            headers["Authorization"] = f"Bearer {config.LLAMA_API_KEY}"
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=config.REQUEST_TIMEOUT,
            headers=headers,
            trust_env=False,  # 本机服务，绕过系统/环境代理设置
        )

    def close(self) -> None:
        self._client.close()

    def health(self) -> tuple[bool, str]:
        try:
            resp = self._client.get("/health", timeout=5)
        except httpx.HTTPError as exc:
            return False, f"连不上 llama-server（{self.base_url}）：{exc.__class__.__name__}"
        if resp.status_code == 200:
            return True, "翻译服务已就绪"
        if resp.status_code == 503:
            return False, "翻译服务正在加载模型，请稍等再试"
        return False, f"翻译服务异常（HTTP {resp.status_code}）"

    def translate(
        self,
        cues: Sequence[Cue],
        target: str = "zh-Hans",
        progress=None,
    ) -> list[str]:
        spec = TARGET_SPECS[target]
        texts = [cue.text for cue in cues]
        total = len(texts)
        results: list[str] = []

        for start in range(0, total, config.BATCH_SIZE):
            batch = texts[start:start + config.BATCH_SIZE]
            context = results[-config.CONTEXT_SIZE:] if config.CONTEXT_SIZE > 0 else []
            try:
                batch_out = self._translate_batch(batch, context, spec)
            except TranslatorError:
                # 批量失败（条数对不上/格式跑偏）时退回逐条翻译
                batch_out = [self._translate_single(text, context, spec) for text in batch]
            results.extend(batch_out)

            if progress:
                done = start + len(batch)
                progress(min(done / total, 1.0), desc=f"翻译 {done}/{total}")

        return results

    def _translate_batch(self, batch: list[str], context: list[str], spec) -> list[str]:
        user = _build_user_message(batch, context)
        raw = self._chat(
            _fill(SYSTEM_PROMPT, spec),
            user,
            max_tokens=len(batch) * (spec[2] * 3 + 12) + 48,
        )
        parsed = _parse_numbered(raw, len(batch))
        if parsed is None:
            raise TranslatorError("译文条数与原文不一致")
        return parsed

    def _translate_single(self, text: str, context: list[str], spec) -> str:
        user = _build_user_message([text], context)
        raw = self._chat(
            _fill(SINGLE_PROMPT, spec),
            user,
            max_tokens=spec[2] * 4 + 32,
        )
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines:
            raise TranslatorError(f"空译文：{text[:40]}")
        cleaned = _strip_number_prefix(lines[0])
        return _clean_text(cleaned)

    def _chat(self, system: str, user: str, max_tokens: int) -> str:
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "top_p": 0.9,
            "max_tokens": max(64, max_tokens),
            "stream": False,
            # Qwen3 默认会先输出思考过程，字幕翻译必须关掉
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            resp = self._client.post("/v1/chat/completions", json=payload)
            if resp.status_code == 400 and "chat_template_kwargs" in payload:
                payload.pop("chat_template_kwargs")
                resp = self._client.post("/v1/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise TranslatorError(
                f"请求 llama-server 失败（{self.base_url}），请确认服务已启动：{exc}"
            ) from exc

        if resp.status_code != 200:
            raise TranslatorError(f"翻译请求失败（HTTP {resp.status_code}）：{resp.text[:300]}")

        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise TranslatorError(f"无法解析模型返回：{resp.text[:300]}") from exc
        return _strip_thinking(content or "")


def _fill(template: str, spec) -> str:
    name, unit, limit, example = spec
    return template.format(target=name, unit=unit, max_chars=limit, example=example)


def _build_user_message(batch: Sequence[str], context: Sequence[str]) -> str:
    parts = []
    if context:
        parts.append("【前文参考（只用于保持连贯，不要翻译）】")
        parts.extend(f"{i + 1}|{line}" for i, line in enumerate(context))
        parts.append("")
    parts.append("【待翻译字幕】")
    parts.extend(f"{i + 1}|{line}" for i, line in enumerate(batch))
    return "\n".join(parts)


def _parse_numbered(raw: str, expected: int) -> list[str] | None:
    hits: dict[int, str] = {}
    for line in raw.splitlines():
        match = _NUMBERED_RE.match(line)
        if not match:
            continue
        index = int(match.group(1))
        if 1 <= index <= expected:
            hits[index] = _clean_text(match.group(2))
    if len(hits) == expected:
        return [hits[i] for i in range(1, expected + 1)]

    # 兜底：模型没带编号，但行数刚好对得上
    lines = [_clean_text(_strip_number_prefix(line)) for line in raw.splitlines() if line.strip()]
    lines = [line for line in lines if line]
    if len(lines) == expected:
        return lines
    return None


def _strip_number_prefix(line: str) -> str:
    return re.sub(r"^\s*(?:-+\s*)?\d{1,3}\s*[|｜:：.、)]\s*", "", line)


def _strip_thinking(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def _clean_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^(译文|翻译)\s*[：:]\s*", "", text)
    text = text.strip().strip('"“”\'‘’')
    return text.strip()
