from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
MODELS_DIR = Path(os.getenv("SUBS_MODELS_DIR", str(BASE_DIR / "models"))).expanduser()
OUTPUT_DIR = Path(os.getenv("SUBS_OUTPUT_DIR", str(BASE_DIR / "output"))).expanduser()
LLAMA_DIR = Path(os.getenv("SUBS_LLAMA_DIR", str(BASE_DIR / "tools" / "llama"))).expanduser()

# 语音识别（faster-whisper）
WHISPER_MODEL = os.getenv("SUBS_WHISPER_MODEL", str(MODELS_DIR / "faster-whisper-large-v3"))
WHISPER_DEVICE = os.getenv("SUBS_WHISPER_DEVICE", "auto")  # auto | cuda | cpu
WHISPER_COMPUTE = os.getenv("SUBS_WHISPER_COMPUTE", "int8_float16")
WHISPER_COMPUTE_CPU = os.getenv("SUBS_WHISPER_COMPUTE_CPU", "int8")

# 翻译（llama.cpp）
LLAMA_SERVER = Path(os.getenv("SUBS_LLAMA_SERVER", str(LLAMA_DIR / "llama-server.exe")))
LLAMA_MODEL = Path(os.getenv("SUBS_LLAMA_MODEL", str(MODELS_DIR / "Qwen3-14B-Q4_K_M.gguf")))
LLAMA_BASE_URL = os.getenv("SUBS_LLAMA_URL", "http://127.0.0.1:18765").rstrip("/")
LLAMA_API_KEY = os.getenv("SUBS_LLAMA_API_KEY", "not-needed")
LLAMA_MODEL_NAME = os.getenv("SUBS_LLAMA_MODEL_NAME", "local")

# 字幕
MAX_SUBTITLE_CHARS = int(os.getenv("SUBS_MAX_CHARS", "16"))  # 每条中文字幕的字数上限
MAX_LATIN_CHARS = int(os.getenv("SUBS_MAX_LATIN_CHARS", "42"))  # 每条英文字幕的字符上限
MAX_CUE_CHARS = int(os.getenv("SUBS_MAX_CUE_CHARS", "44"))  # 单条原文的字数上限
MAX_CUE_SECONDS = float(os.getenv("SUBS_MAX_CUE_SECONDS", "7"))
MIN_CUE_SECONDS = float(os.getenv("SUBS_MIN_CUE_SECONDS", "1"))
BATCH_SIZE = int(os.getenv("SUBS_BATCH_SIZE", "8"))  # 每次送给模型的字幕条数
CONTEXT_SIZE = int(os.getenv("SUBS_CONTEXT_SIZE", "2"))  # 附带的已译上文条数
REQUEST_TIMEOUT = float(os.getenv("SUBS_REQUEST_TIMEOUT", "120"))

LANGUAGES = {
    "自动识别": None,
    "普通话": "zh",
    "粤语": "yue",
    "日语": "ja",
    "韩语": "ko",
    "英语": "en",
    "法语": "fr",
    "德语": "de",
    "西班牙语": "es",
    "俄语": "ru",
}

# 字幕语言：键是界面选项，值是每行字幕对应的语言代码（source 表示识别原文）
SUBTITLE_LANGUAGES = {
    "源语言": ("source",),
    "英文": ("en",),
    "简体中文": ("zh-Hans",),
    "繁体中文": ("zh-Hant",),
    "源语言+英文": ("source", "en"),
    "源语言+简体中文": ("source", "zh-Hans"),
    "源语言+繁体中文": ("source", "zh-Hant"),
    "英文+简体中文": ("en", "zh-Hans"),
    "英文+繁体中文": ("en", "zh-Hant"),
}


def ensure_dirs() -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def whisper_model_ready() -> bool:
    path = Path(WHISPER_MODEL)
    return (path / "model.bin").exists() or path.exists() and path.suffix == ".bin"
