#!/usr/bin/env python3
"""Generate a voiceover MP3 for an Equanimous script, with Chatterbox Nano
as the main provider and Kokoro as the fallback.

Chatterbox Nano (Resemble AI, MIT licence) clones a short reference
recording of a real voice (5-10 s) and is the more expressive, humanlike
narrator. Kokoro (Apache 2.0) is the reliable fallback with a fixed preset
voice. The provider used is recorded in the JSON summary.

Runs on GitHub Actions (ubuntu-latest) via the chatterbox-voiceover.yml
workflow. Nothing is installed on any device or server: the GitHub runner
installs the packages, downloads the models, synthesizes the audio, and
uploads the MP3 as an artifact. n8n triggers the workflow and downloads
the result, and the GitHub Pages voice site triggers it from the browser.

Usage:
  python3 chatterbox-voiceover.py --text-file script.txt \
    --reference references/my-voice.wav --speed 0.9 --provider auto \
    --kokoro-voice bm_daniel --kokoro-lang en-gb \
    --model kokoro-models/kokoro-v1.0.onnx \
    --voices kokoro-models/voices-v1.0.bin \
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


def split_sentences(text):
    """Split text into sentences on . ! ? (keeping the punctuation)."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def synth_chatterbox(sentences, reference, sr_target=24000):
    """Render with Chatterbox Nano (CPU), cloned from the reference file.

    Returns (audio float32 numpy array, sample_rate). Raises on failure.
    """
    import torchaudio as ta
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    model = ChatterboxTurboTTS.from_pretrained(device="cpu", nano=True)
    chunks = []
    sr = model.sr
    for s in sentences:
        wav = model.generate(s, audio_prompt_path=reference)
        arr = wav.squeeze(0).detach().cpu().numpy().astype(np.float32)
        chunks.append(arr)
        chunks.append(np.zeros(int(sr * 0.15), dtype=np.float32))
    if len(chunks) > 1:
        chunks = chunks[:-1]  # drop trailing pause
    audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    if sr != sr_target:
        # Resample to 24 kHz with torchaudio for a consistent output.
        import torch
        tensor = torch.from_numpy(audio).unsqueeze(0)
        resampled = ta.functional.resample(tensor, sr, sr_target)
        audio = resampled.squeeze(0).numpy().astype(np.float32)
        sr = sr_target
    return audio, sr


def synth_kokoro(sentences, voice, lang, speed, model_path, voices_path):
    """Render with Kokoro (fallback). Returns (audio, sample_rate)."""
    from kokoro_onnx import Kokoro

    kokoro = Kokoro(model_path, voices_path)
    chunks = []
    sr = None
    for s in sentences:
        samples, sr = kokoro.create(s, voice=voice, speed=speed, lang=lang)
        chunks.append(np.asarray(samples, dtype=np.float32))
        chunks.append(np.zeros(int(sr * 0.15), dtype=np.float32))
    if len(chunks) > 1:
        chunks = chunks[:-1]  # drop trailing pause
    audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    return audio, sr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default=None)
    ap.add_argument("--text-file", default=None)
    ap.add_argument("--reference", default=None,
                    help="voice-clone reference audio (5-10 s WAV/MP3) for Chatterbox")
    ap.add_argument("--speed", type=float, default=0.9,
                    help="Kokoro speed; Chatterbox has no speed control (ignored there)")
    ap.add_argument("--provider", default="auto",
                    choices=["auto", "chatterbox", "kokoro"],
                    help="auto: Chatterbox Nano first, Kokoro on failure")
    ap.add_argument("--kokoro-voice", default="bm_daniel")
    ap.add_argument("--kokoro-lang", default="en-gb")
    ap.add_argument("--model", default="kokoro-models/kokoro-v1.0.onnx")
    ap.add_argument("--voices", default="kokoro-models/voices-v1.0.bin")
    ap.add_argument("--video-id", required=True)
    ap.add_argument("--out-dir", default="out")
    args = ap.parse_args()

    if args.text_file:
        with open(args.text_file, encoding="utf-8") as f:
            text = f.read().strip()
    else:
        text = (args.text or "").strip()
    if not text:
        print("empty script text", file=sys.stderr)
        return 1

    sentences = split_sentences(text)
    provider_used = None
    provider_error = None
    audio = None
    sr = None

    # Main provider: Chatterbox Nano (needs a reference file).
    if args.provider in ("auto", "chatterbox"):
        if args.reference and os.path.exists(args.reference):
            try:
                audio, sr = synth_chatterbox(sentences, args.reference)
                provider_used = "chatterbox-nano"
            except Exception as e:  # noqa: BLE001 - fall back to Kokoro
                provider_error = "chatterbox failed: %s" % e
                print(provider_error, file=sys.stderr)
        else:
            provider_error = "chatterbox skipped: no reference file (%s)" % args.reference
            print(provider_error, file=sys.stderr)
        if args.provider == "chatterbox" and provider_used is None:
            print("chatterbox requested but failed and no fallback allowed",
                  file=sys.stderr)
            return 1

    # Fallback (or forced): Kokoro.
    if provider_used is None:
        try:
            audio, sr = synth_kokoro(sentences, args.kokoro_voice,
                                     args.kokoro_lang, args.speed,
                                     args.model, args.voices)
            provider_used = "kokoro-%s" % args.kokoro_voice
        except Exception as e:  # noqa: BLE001 - nothing left to try
            print("kokoro fallback failed: %s" % e, file=sys.stderr)
            return 1

    duration = float(len(audio) / sr) if sr else 0.0
    os.makedirs(args.out_dir, exist_ok=True)
    wav_path = os.path.join(args.out_dir, args.video_id + "-voiceover.wav")
    mp3_path = os.path.join(args.out_dir, args.video_id + "-voiceover.mp3")
    sf.write(wav_path, audio, sr)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", wav_path,
         "-codec:a", "libmp3lame", "-b:a", "128k", mp3_path],
        check=True,
    )
    os.remove(wav_path)

    summary = {
        "video_id": args.video_id,
        "provider": provider_used,
        "provider_error": provider_error,
        "reference": args.reference,
        "kokoro_voice": args.kokoro_voice,
        "kokoro_lang": args.kokoro_lang,
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
