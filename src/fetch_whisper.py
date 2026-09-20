"""Download an OpenAI Whisper checkpoint into the project-local cache."""

from __future__ import annotations

import argparse

import model_paths
import whisper


def main() -> int:
    parser = argparse.ArgumentParser(description="Download a Whisper model to models/whisper")
    parser.add_argument("--model", default="large-v3", choices=sorted(whisper._MODELS))
    args = parser.parse_args()
    model_paths.WHISPER_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading Whisper {args.model} to {model_paths.WHISPER_DIR}", flush=True)
    whisper.load_model(args.model, device="cpu", download_root=str(model_paths.WHISPER_DIR))
    print(f"Whisper {args.model} is ready.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
