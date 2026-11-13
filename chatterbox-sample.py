"""Generate voice samples with Chatterbox Nano (Resemble AI, MIT licence),
cloned from a short reference recording of a real male voice you have the
right to use. Runs on GitHub Actions (a CPU runner); nothing is installed
on any device or server.

Nano is the 110M CPU variant of Chatterbox: it shares Turbo's architecture
(single-step decoder, native paralinguistic tags like [laugh] / [chuckle])
and is loaded through ChatterboxTurboTTS with nano=True. It needs a
reference clip (5-10 s) for voice cloning; without one it has no preset
male voice. Every output carries Resemble's PerTh watermark (inaudible).

The reference used for the first samples is a public-domain LibriVox
recording: Ralph Snelson reading Jules Verne, "Around the World in
Eighty Days" (chapter 1). LibriVox dedicates its recordings to the public
domain. For production, replace it with a recording of the voice you
actually want (e.g. your own voice reading two motivational lines with
the exact energy you want).

Each named text is rendered once, so the samples are directly comparable
with the Kokoro voice samples (same 4-sentence text).
"""
import argparse, json, os, subprocess

import torchaudio as ta
from chatterbox.tts_turbo import ChatterboxTurboTTS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--reference', required=True,
                    help='5-10 s reference WAV/MP3 of the voice to clone')
    ap.add_argument('--texts-json', required=True,
                    help='JSON list of {"name": ..., "text": ...} samples to render')
    ap.add_argument('--out-dir', default='out')
    args = ap.parse_args()

    with open(args.texts_json, encoding='utf-8') as f:
        items = json.load(f)
    assert items, 'no sample texts given'
    assert os.path.exists(args.reference), 'reference file not found: %s' % args.reference

    model = ChatterboxTurboTTS.from_pretrained(device='cpu', nano=True)
    os.makedirs(args.out_dir, exist_ok=True)
    results = []
    for item in items:
        name = item['name']
        text = item['text'].strip()
        assert text, 'empty text for sample %s' % name
        wav = model.generate(text, audio_prompt_path=args.reference)
        wav_path = os.path.join(args.out_dir, name + '.wav')
        ta.save(wav_path, wav, model.sr)
        mp3 = os.path.join(args.out_dir, name + '.mp3')
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', wav_path,
                        '-codec:a', 'libmp3lame', '-b:a', '128k', mp3], check=True)
        os.remove(wav_path)
        import soundfile as sf
        info = sf.info(mp3)
        results.append({'name': name, 'output': mp3,
                        'duration': round(float(info.duration), 3),
                        'sample_rate': int(info.samplerate),
                        'words': len(text.split())})
    print(json.dumps({'model': 'chatterbox-nano', 'reference': args.reference,
                      'samples': results}))


if __name__ == '__main__':
    main()
