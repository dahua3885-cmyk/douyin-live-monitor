"""Explicit one-time model download; the transcription worker remains offline."""
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="下载本地 Whisper small 转写模型")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "models" / "whisper-small")
    args = parser.parse_args()
    from huggingface_hub import snapshot_download
    target = args.output.expanduser().resolve()
    snapshot_download(
        repo_id="Systran/faster-whisper-small", local_dir=str(target),
        allow_patterns=["*.json", "model.bin", "vocabulary.*", "README.md", "LICENSE*"],
    )
    for name in ("model.bin", "config.json", "tokenizer.json"):
        if not (target / name).is_file():
            raise RuntimeError(f"下载不完整：缺少 {name}，请重试")
    print(f"Model ready: {target}")


if __name__ == "__main__":
    main()
