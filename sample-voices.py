"""Generate short voice samples for several Kokoro voices (one MP3 each),
so Hriduan can hear and choose a narrator voice. Runs on GitHub Actions
(a CPU runner); nothing is installed on any device or server.

Each voice reads the SAME sample text, so the comparison is fair. Language
is picked from the voice id: bm_* (British) -> en-gb, am_* (American) ->
en-us. Speed is shared across voices; slow it slightly (0.9-0.95) for a
more deliberate, natural delivery once a voice is chosen.
"""
import argparse, json, os, re, subprocess
import numpy as np
import soundfile as sf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--text', default=None)
    ap.add_argument('--text-file', default=None)
    ap.add_argument('--voices', required=True, help='comma-separated Kokoro voice ids')
    ap.add_argument('--speed', type=float, default=1.0)
    ap.add_argument('--model', default='kokoro-models/kokoro-v1.0.onnx')
    ap.add_argument('--voices-file', default='kokoro-models/voices-v1.0.bin')
    ap.add_argument('--out-dir', default='out')
    args = ap.parse_args()
    if args.text_file:
        text = open(args.text_file, encoding='utf-8').read().strip()
    else:
        text = (args.text or '').strip()
    assert text, 'no sample text given'
    from kokoro_onnx import Kokoro
    kokoro = Kokoro(args.model, args.voices_file)
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]
    os.makedirs(args.out_dir, exist_ok=True)
    results = []
    for voice in [v.strip() for v in args.voices.split(',') if v.strip()]:
        lang = 'en-gb' if voice.startswith('bm_') else 'en-us'
        chunks = []
        sr = 24000
        for s in sentences:
            samples, sr = kokoro.create(s, voice=voice, speed=args.speed, lang=lang)
            chunks.append(np.asarray(samples, dtype=np.float32))
        silence = np.zeros(int(0.15 * sr), dtype=np.float32)
        parts = []
        for c in chunks:
            parts.append(c)
            parts.append(silence)
        audio = np.concatenate(parts[:-1]) if parts else np.zeros(0, dtype=np.float32)
        wav = os.path.join(args.out_dir, voice + '-sample.wav')
        sf.write(wav, audio, sr)
        mp3 = os.path.join(args.out_dir, voice + '-sample.mp3')
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', wav,
                        '-codec:a', 'libmp3lame', '-b:a', '128k', mp3], check=True)
        os.remove(wav)
        results.append({'voice': voice, 'lang': lang, 'output': mp3,
                        'duration': round(float(len(audio)) / sr, 3)})
    print(json.dumps({'samples': results, 'speed': args.speed,
                      'words': len(text.split()), 'sentences': len(sentences)}))


if __name__ == '__main__':
    main()
