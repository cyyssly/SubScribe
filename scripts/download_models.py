"""下载本项目需要的本地模型（支持断点续传）。

用法：
    python scripts/download_models.py --all
    python scripts/download_models.py --gguf
    python scripts/download_models.py --whisper --endpoint https://huggingface.co
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

GGUF_REPO = "Qwen/Qwen3-14B-GGUF"
GGUF_FILE = "Qwen3-14B-Q4_K_M.gguf"
WHISPER_REPO = "Systran/faster-whisper-large-v3"
DEFAULT_ENDPOINT = "https://hf-mirror.com"


def download_gguf(endpoint: str, repo: str, filename: str) -> Path:
    os.environ["HF_ENDPOINT"] = endpoint
    from huggingface_hub import hf_hub_download

    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[GGUF] {repo}/{filename} -> {config.MODELS_DIR}")
    path = hf_hub_download(
        repo_id=repo,
        filename=filename,
        local_dir=str(config.MODELS_DIR),
    )
    return Path(path)


def download_whisper(endpoint: str, repo: str) -> Path:
    os.environ["HF_ENDPOINT"] = endpoint
    from huggingface_hub import snapshot_download

    target = Path(config.WHISPER_MODEL)
    target.mkdir(parents=True, exist_ok=True)
    print(f"[Whisper] {repo} -> {target}")
    snapshot_download(
        repo_id=repo,
        local_dir=str(target),
        max_workers=4,
        ignore_patterns=["*.md", ".gitattributes"],
    )
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="下载视频字幕所需的本地模型")
    parser.add_argument("--all", action="store_true", help="下载全部模型（默认）")
    parser.add_argument("--gguf", action="store_true", help="只下载翻译模型")
    parser.add_argument("--whisper", action="store_true", help="只下载语音识别模型")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Hugging Face 镜像地址")
    parser.add_argument("--repo", default=GGUF_REPO, help="GGUF 仓库")
    parser.add_argument("--file", default=GGUF_FILE, help="GGUF 文件名")
    args = parser.parse_args()

    do_all = args.all or not (args.gguf or args.whisper)

    if do_all or args.gguf:
        path = download_gguf(args.endpoint, args.repo, args.file)
        print(f"[完成] 翻译模型：{path}（{path.stat().st_size / 2**30:.2f} GB）")

    if do_all or args.whisper:
        path = download_whisper(args.endpoint, WHISPER_REPO)
        model_bin = path / "model.bin"
        size = model_bin.stat().st_size / 2**30 if model_bin.exists() else 0
        print(f"[完成] 语音模型：{path}（model.bin {size:.2f} GB）")

    print("全部模型已就绪，可运行 app.py")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断，重新运行会从断点继续。")
        raise SystemExit(130)
