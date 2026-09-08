"""Offline ASR subprocess: one JSON request/response per line. No remote API."""
import json
import os
from pathlib import Path
import sys
import time

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")


def find_local_model():
    explicit = os.environ.get("LIVE_TRANSCRIBE_MODEL_DIR")
    if explicit:
        candidate = Path(explicit)
        if (candidate / "model.bin").is_file():
            return candidate
        raise RuntimeError("指定的本地语音模型目录不完整")
    bundled = Path(__file__).resolve().parent / "models" / "whisper-small"
    if all((bundled / name).is_file() for name in ("model.bin", "config.json", "tokenizer.json")):
        return bundled
    hub = Path(os.environ.get("HF_HUB_CACHE", str(Path.home() / ".cache/huggingface/hub")))
    for candidate in sorted((hub / "models--Systran--faster-whisper-small/snapshots").glob("*")):
        if all((candidate / name).is_file() for name in ("model.bin", "config.json", "tokenizer.json")):
            return candidate
    raise RuntimeError("未找到已下载的 Whisper small 模型；请在本机安装模型后重试")


def main():
    model = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            path = Path(request["path"])
            if not path.is_file() or path.suffix.lower() != ".wav":
                raise ValueError("音频片段不存在或格式不正确")
            began = time.monotonic()
            if model is None:
                from faster_whisper import WhisperModel
                model = WhisperModel(str(find_local_model()), device="cpu", compute_type="int8", cpu_threads=4, num_workers=1, local_files_only=True)
            segments, info = model.transcribe(str(path), language="zh", beam_size=3,
                vad_filter=True, condition_on_previous_text=False,
                initial_prompt="以下是普通话直播的原话，使用简体中文。",
                vad_parameters={"min_silence_duration_ms": 500})
            result = []
            for seg in segments:
                text = seg.text.strip()
                if text:
                    result.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": text})
            output = {"ok": True, "segments": result, "duration": round(info.duration, 3), "model": "Whisper small / CPU INT8", "elapsed_seconds": round(time.monotonic()-began, 3)}
        except Exception as exc:
            # Errors contain local model/setup information only, never stream URLs.
            output = {"ok": False, "error": str(exc)[:300], "type": type(exc).__name__}
        print(json.dumps(output, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
