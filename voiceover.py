#!/usr/bin/env python3
"""Generate a Kokoro voiceover MP3 for an Equanimous script.

Runs on GitHub Actions (ubuntu-latest) via the voiceover.yml workflow.
Nothing is installed on any device or server: the GitHub runner installs
kokoro-onnx, downloads the model files, synthesizes the audio, and uploads
the MP3 as an artifact. n8n triggers the workflow and downloads the result.

Usage:
  python3 voiceover.py --text-file script.txt --voice bm_daniel --lang en-gb \
    --speed 1.0 --model kokoro-v1.0.onnx --voices voices-v1.0.bin \
    --video-id EQ-1794354039014-0

Output: out/<video-id>-voiceover.mp3 (24 kHz mono MP3) + JSON summary on stdout.
"""
import argparse
import json
import os
import re
import subprocess
import sys

import numpy as np
import soundfile as sf

from kokoro_onnx import Kokoro


def split_sentences(text):
    """Split text into sentences on . ! ? (keeping the punctuation)."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text-file", required=True)
    ap.add_argument("--voice", default="bm_daniel")
    ap.add_argument("--lang", default="en-gb")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--model", required=True)
    ap.add_argument("--voices", required=True)
    ap.add_argument("--video-id", required=True)
    args = ap.parse_args()

    with open(args.text_file, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        print("empty script text", file=sys.stderr)
        return 1

    kokoro = Kokoro(args.model, args.voices)

    # Synthesize sentence by sentence (robust for long scripts) and join
    # with a short natural pause between sentences.
    sentences = split_sentences(text)
    chunks = []
    sr = None
    for s in sentences:
        samples, sr = kokoro.create(
            s, voice=args.voice, speed=args.speed, lang=args.lang
        )
        chunks.append(np.asarray(samples, dtype=np.float32))
        chunks.append(np.zeros(int(sr * 0.15), dtype=np.float32))
    if len(chunks) > 1:
        chunks = chunks[:-1]  # drop trailing pause
    audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    duration = float(len(audio) / sr) if sr else 0.0

    os.makedirs("out", exist_ok=True)
    wav_path = f"out/{args.video_id}-voiceover.wav"
    mp3_path = f"out/{args.video_id}-voiceover.mp3"
    sf.write(wav_path, audio, sr)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", wav_path,
         "-codec:a", "libmp3lame", "-b:a", "128k", mp3_path],
        check=True,
    )
    os.remove(wav_path)

    summary = {
        "video_id": args.video_id,
        "voice": args.voice,
        "lang": args.lang,
        "speed": args.speed,
        "output": mp3_path,
        "duration": round(duration, 3),
        "sample_rate": sr,
        "words": len(text.split()),
        "sentences": len(sentences),
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
