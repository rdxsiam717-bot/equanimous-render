#!/usr/bin/env python3
"""Equanimous render runner (GitHub Actions).

Self-contained port of the local FFmpeg bridge
(~/workspace/equanimous/bridge/bridge.py) for GitHub Actions runners.
The bridge cannot be reached from n8n Cloud (its public tunnel URL is
dead), so the same assembly / caption logic runs here instead, on a
free GitHub-hosted runner, triggered by n8n via workflow_dispatch.

Two modes (matching n8n workflows W05 and W06):
  assemble  - build a 1080x1920 h264+aac MP4 from an ordered media list,
              a narration audio file and an optional per-scene ambience
              list. This is bridge /assemble.
  captions  - measure word timings from a narration with faster-whisper,
              build the karaoke ASS and burn it into a video. This is
              bridge /measure-captions + /captions in one run (W06).

Usage:
  render.py --mode assemble --manifest manifest.json --video-id EQ-... \
            --output out/EQ-....mp4
  render.py --mode captions --manifest manifest.json --video-id EQ-... \
            --output out/EQ-...-captioned.mp4 \
            --drive-key service-account.json --drive-folder <folder-id>

--manifest accepts a path to a JSON file OR a JSON string (the workflow
passes the workflow_dispatch manifest_json input this way).

Manifest shapes:
  assemble:
    {
      "job_id": "EQ-1794202623342-2",            # optional, defaults to video_id
      "media": [ <item>, ... ],                   # REQUIRED, ordered list
      "narration": <item>,                        # REQUIRED, single audio item
      "ambience": [ <item> | null, ... ],         # optional, one per scene
      "srt_text": "...",                          # optional plain SRT
      "ass_text": "..."                           # optional karaoke ASS (wins over SRT)
    }
  captions:
    {
      "job_id": "EQ-1794202623342-2",
      "video": <item>,                            # REQUIRED, the assembled MP4
      "narration": <item>,                        # REQUIRED, the narration audio
      "script": "Do bad thoughts hold you back? ..."  # REQUIRED, script text
    }

A media item is one of:
  {"url": "https://..."}                          # downloaded
  {"path": "/local/file.mp4"}                     # copied (runner-local file)
  {"data": "<base64>", "ext": "mp4"}              # base64 payload
  "<base64>"                                      # bare base64 string
  "/local/file.mp4"                               # bare local path
  {"drive": "<file-id>", "ext": "mp4"}            # Google Drive file
                                                  # (service account key
                                                  # via --drive-key)
  null                                            # ambience only: silence for that scene

Rules preserved exactly from the bridge (do not "improve" them):
  * Output is always 1080x1920 vertical (9:16), H.264 + AAC.
  * Output duration is always 30-60 seconds. Anything outside that
    range exits non-zero (exit code 2) with a clear error — this is the
    bridge's HTTP 422.
  * Scene changes use a gentle 0.5s cross-dissolve (xfade=fade), never a
    hard cut. Fade in/out 0.5s at the ends. A vignette (PI/4) is applied.
  * A media item is a clip only when its probed duration > 0.5s; stills
    probe as ~0.04s and get a slow push-in (zoompan 1.00 -> 1.10).
    Clip inputs carry -t <share>. setsar=1 on every segment. 4K inputs
    are normalised alone before assembly (scale/crop 1080x1920, fps 30,
    crf 23) — never in the big filtergraph (that OOM-kills the encode).
  * Chained-xfade offset = k*(seg - XFADE), relative to each step's
    first input, where seg = (audio_dur + (n-1)*0.5)/n.
  * Ambience: per segment, trimmed to its output span, fed 1.0s longer,
    volume 0.2, resampled 44100 Hz stereo, first piece fades in 0.5s,
    last fades out 0.5s, consecutive pieces joined by acrossfade d=1.0
    triangular; narration mixed with amix normalize=0; null slots are
    silence. Fetch failures are a hard error.
  * Caption style v3: real ASS (PlayRes 1080x1920; Montserrat ExtraBold;
    gold &H0000D7FF, navy outline &H00800000; Alignment 5; size 55,
    outline 2.5, shadow 1, BorderStyle 4 soft translucent dark box
    &H66000000; {\\blur0.6} per line). Karaoke: one dialogue line per
    word, spoken word bright gold, others dim bronze &H00608AC8,
    flipping exactly on measured onsets; NO fade anywhere.
  * Chunker v3: sentences at . ? !, phrases at commas, 2-4 word cues
    (trailing-1-word merge-back guard); cue start = first word onset,
    end = last word end. Degenerate-timing sanitizer: a word with
    end <= start gets a 0.12s slice, later words pushed after it.
  * 200 MB total download cap, 50 MB per media item.

Only the Python standard library plus faster-whisper (word timings) is
used. ffmpeg/ffprobe and the Montserrat font are provided by the runner
(the workflow installs them).

Exit codes: 0 = success (a JSON summary is printed on stdout),
1 = general failure, 2 = duration outside 30-60s (bridge HTTP 422).
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

MAX_BODY_BYTES = 200 * 1024 * 1024  # 200 MB total download cap
MAX_ITEM_BYTES = 50 * 1024 * 1024   # 50 MB per media item
FFMPEG_TIMEOUT = 600  # seconds per encode
WHISPER_TIMEOUT = 300  # seconds per caption measurement

XFADE = 0.5  # seconds of gentle cross-dissolve between scenes
ACROSS = 1.0  # seconds of crossfade between ambience pieces

# Caption style v3 (Hriduan, 2026-11-09): MIDDLE of the frame, stylish and
# modern — Montserrat ExtraBold, gold with a soft navy outline and a
# gentle glow, on a soft translucent dark box so it reads on any scene.
# 2-4 words at a time, karaoke highlight (spoken word bright gold, the
# rest dim bronze). Captions are written as a real ASS file with PlayRes
# 1080x1920 so sizes are absolute pixels: Fontsize 55 matches the
# victory short's scale. (ffmpeg's subtitles force_style IGNORES
# Alignment on this build — every force_style test rendered top-left —
# so the ASS file is built directly instead.)
# Copied verbatim from bridge.py ASS_HEADER.
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Montserrat ExtraBold,55,&H0000D7FF,&H000000FF,&H00800000,&H66000000,-1,0,0,0,100,100,0,0,4,2.5,1,5,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# Where the Montserrat ExtraBold font may live on the runner. The
# workflow installs it to /usr/share/fonts/truetype/montserrat/; the
# ~/.fonts path is the bridge's original location, kept for parity.
FONT_PATHS = [
    os.path.expanduser("~/.fonts/Montserrat-ExtraBold.ttf"),
    "/usr/share/fonts/truetype/montserrat/Montserrat-ExtraBold.ttf",
    "/usr/local/share/fonts/Montserrat-ExtraBold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

GOLD = r"{\1c&H0000D7FF&}"  # bright gold: the word being spoken
DIM = r"{\1c&H00608AC8&}"   # dim bronze-gold: the other words
PRE = r"{\blur0.6}"

# Running total of downloaded/copied bytes for this run (the bridge's
# 200 MB body cap, applied here to everything fetched).
_TOTAL_BYTES = [0]


class RenderError(Exception):
    """Fatal render error. code 2 = duration outside 30-60s (bridge 422)."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def log(msg):
    """Progress goes to stderr so stdout stays a clean JSON summary."""
    print("[render] %s" % msg, file=sys.stderr, flush=True)


def run(cmd, timeout=60):
    """Run a command, return (returncode, stdout, stderr)."""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def check_tools():
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RenderError("%s not found on PATH — the workflow must "
                              "install it" % tool)


def find_font():
    """Return the first usable caption font path, or None."""
    override = os.environ.get("EQUANIMOUS_FONT")
    if override and os.path.exists(override):
        return override
    for path in FONT_PATHS:
        if os.path.exists(path):
            return path
    return None


def probe_duration(path):
    """Return media duration in seconds via ffprobe, or None."""
    rc, out, _err = run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", path,
    ])
    if rc != 0:
        return None
    try:
        return float(json.loads(out)["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return None


def probe_video(path):
    """Return (width, height, duration) of a video file, or Nones."""
    rc, out, _err = run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", path,
    ])
    if rc != 0:
        return None, None, None
    try:
        data = json.loads(out)
        stream = next(s for s in data["streams"] if s["codec_type"] == "video")
        return (int(stream["width"]), int(stream["height"]),
                float(data["format"]["duration"]))
    except (KeyError, TypeError, ValueError, StopIteration):
        return None, None, None


def upper_srt(srt_text):
    """Force ALL CAPS on subtitle dialogue lines, keep timings intact."""
    lines = []
    for line in srt_text.splitlines():
        if re.match(r"^\s*\d+\s*$", line):
            lines.append(line)  # cue number
        elif re.match(r"^\s*\d\d:\d\d:\d\d", line):
            lines.append(line)  # timing line
        elif line.strip() == "":
            lines.append(line)
        else:
            lines.append(line.upper())
    return "\n".join(lines) + "\n"


def srt_to_ass(srt_text):
    """Convert SRT cues (already ALL CAPS) into an ASS file with the
    locked caption style. Returns the ASS document text."""
    cues = []
    blocks = re.split(r"\n\s*\n", srt_text.strip())
    for block in blocks:
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 3:
            continue
        timing = lines[1]
        m = re.match(r"(\d\d):(\d\d):(\d\d)[,.](\d\d\d)\s*-->\s*"
                     r"(\d\d):(\d\d):(\d\d)[,.](\d\d\d)", timing)
        if not m:
            continue
        (h1, m1, s1, ms1, h2, m2, s2, ms2) = [int(x) for x in m.groups()]
        start = "%d:%02d:%02d.%02d" % (h1, m1, s1, ms1 // 10)
        end = "%d:%02d:%02d.%02d" % (h2, m2, s2, ms2 // 10)
        text = " ".join(lines[2:]).replace("{", "").replace("}", "")
        cues.append((start, end, text))
    out = [ASS_HEADER]
    for start, end, text in cues:
        out.append("Dialogue: 0,%s,%s,Default,,0,0,0,,{\\blur0.6}%s"
                   % (start, end, text))
    return "\n".join(out) + "\n"


def _charge(nbytes):
    """Enforce the 200 MB total download cap across the whole run."""
    _TOTAL_BYTES[0] += nbytes
    if _TOTAL_BYTES[0] > MAX_BODY_BYTES:
        raise RenderError("total downloaded media exceeds the 200 MB cap")


def _item_ext(item, default):
    """Per-item extension: URL items take their suffix (.mp4/.jpg/...),
    dict items may carry "ext", local paths take their own suffix,
    otherwise the default ext is used."""
    if isinstance(item, dict) and "url" in item:
        suffix = item["url"].split("?")[0].rsplit(".", 1)[-1].lower()
        if suffix in ("mp4", "mov", "webm", "jpg", "jpeg", "png", "webp",
                      "mp3", "wav", "m4a", "ogg", "flac"):
            return "jpg" if suffix == "jpeg" else suffix
    elif isinstance(item, dict) and "ext" in item:
        return re.sub(r"[^a-z0-9]", "", str(item["ext"]).lower()) or default
    elif isinstance(item, dict) and "path" in item:
        suffix = str(item["path"]).rsplit(".", 1)[-1].lower()
        if suffix:
            return suffix
    elif isinstance(item, str) and os.path.isfile(item):
        suffix = item.rsplit(".", 1)[-1].lower()
        if suffix:
            return suffix
    return default


def _fetch_one(item, tmpdir, prefix, index, ext):
    """Resolve one media item to a file on disk. Returns the path, or
    None for a null ambience slot (silence). Raises RenderError on any
    fetch failure — a video must never ship with the wrong scene's
    sound or a missing scene."""
    if item is None:
        return None
    item_ext = _item_ext(item, ext)
    dest = os.path.join(tmpdir, "%s%d.%s" % (prefix, index, item_ext))
    if isinstance(item, dict) and "path" in item:
        src = item["path"]
        if not os.path.isfile(src):
            raise RenderError("%s %d: local file not found: %s"
                              % (prefix, index, src))
        size = os.path.getsize(src)
        if size > MAX_ITEM_BYTES:
            raise RenderError("%s %d: too large (%d bytes)"
                              % (prefix, index, size))
        _charge(size)
        shutil.copyfile(src, dest)
    elif isinstance(item, str) and os.path.isfile(item):
        size = os.path.getsize(item)
        if size > MAX_ITEM_BYTES:
            raise RenderError("%s %d: too large (%d bytes)"
                              % (prefix, index, size))
        _charge(size)
        shutil.copyfile(item, dest)
    elif isinstance(item, str):
        raw = base64.b64decode(item, validate=False)
        if len(raw) > MAX_ITEM_BYTES:
            raise RenderError("%s %d: too large" % (prefix, index))
        _charge(len(raw))
        with open(dest, "wb") as fh:
            fh.write(raw)
    elif isinstance(item, dict) and "data" in item:
        raw = base64.b64decode(item["data"], validate=False)
        if len(raw) > MAX_ITEM_BYTES:
            raise RenderError("%s %d: too large" % (prefix, index))
        _charge(len(raw))
        with open(dest, "wb") as fh:
            fh.write(raw)
    elif isinstance(item, dict) and "url" in item:
        req = urllib.request.Request(item["url"], headers={
            "User-Agent": "EquanimousRender/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read(MAX_BODY_BYTES)
        if len(raw) > MAX_ITEM_BYTES:
            raise RenderError("%s %d: too large" % (prefix, index))
        _charge(len(raw))
        with open(dest, "wb") as fh:
            fh.write(raw)
    elif isinstance(item, dict) and "drive" in item:
        _drive_download(str(item["drive"]), dest)
    else:
        raise RenderError("%s %d: unsupported item format" % (prefix, index))
    return dest



# ---------------------------------------------------------------------------
# Google Drive access (service account).
# The runner needs Drive because n8n passes media by Drive file ID (the
# files are private; base64 in the dispatch input would be far too large).
# Auth is a service account key (GitHub Secret GOOGLE_SERVICE_ACCOUNT_KEY,
# written to a file by the workflow). google-auth + requests are installed
# by the workflow and only imported when a drive item is used, so local
# URL/base64 runs still work without them.
# ---------------------------------------------------------------------------

_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
_drive_creds = [None]
_drive_key_path = [None]


def _drive_credentials():
    """Load the service account credentials (cached)."""
    if _drive_creds[0] is not None:
        return _drive_creds[0]
    try:
        from google.oauth2 import service_account
    except ImportError:
        raise RenderError(
            "google-auth is not installed — the workflow must "
            "pip install google-auth requests")
    key_path = _drive_key_path[0] or os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_KEY_FILE") or ""
    key_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or ""
    if key_path and os.path.isfile(key_path):
        creds = service_account.Credentials.from_service_account_file(
            key_path, scopes=_DRIVE_SCOPES)
    elif key_json:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(key_json), scopes=_DRIVE_SCOPES)
    else:
        raise RenderError(
            "no Drive service account key — pass --drive-key with the "
            "service account JSON path (or set "
            "GOOGLE_SERVICE_ACCOUNT_KEY_FILE)")
    _drive_creds[0] = creds
    return creds


def _drive_token():
    """Return a fresh OAuth2 access token for the Drive API."""
    try:
        from google.auth.transport.requests import Request as _AuthRequest
    except ImportError:
        raise RenderError(
            "google-auth is not installed — the workflow must "
            "pip install google-auth requests")
    creds = _drive_credentials()
    if not creds.valid:
        creds.refresh(_AuthRequest())
    return creds.token


def _drive_download(file_id, dest):
    """Download a Drive file by ID to dest. Raises RenderError."""
    import requests as _requests
    token = _drive_token()
    url = ("https://www.googleapis.com/drive/v3/files/%s?alt=media"
           % file_id)
    resp = _requests.get(url, headers={"Authorization": "Bearer " + token},
                         timeout=180)
    if resp.status_code != 200:
        raise RenderError("Drive download failed for %s: HTTP %d %s"
                          % (file_id, resp.status_code, resp.text[:200]))
    if len(resp.content) > MAX_ITEM_BYTES:
        raise RenderError("Drive file %s too large" % file_id)
    _charge(len(resp.content))
    with open(dest, "wb") as fh:
        fh.write(resp.content)
    return dest


def _drive_upload(local_path, name, folder_id):
    """Upload a file to a Drive folder. Returns the new file ID."""
    import requests as _requests
    token = _drive_token()
    meta = {"name": name}
    if folder_id:
        meta["parents"] = [folder_id]
    with open(local_path, "rb") as fh:
        data = fh.read()
    boundary = "equanimous-render-boundary"
    body = (
        ("--%s\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
         % boundary).encode()
        + json.dumps(meta).encode()
        + ("\r\n--%s\r\nContent-Type: video/mp4\r\n\r\n"
           % boundary).encode()
        + data
        + ("\r\n--%s--\r\n" % boundary).encode()
    )
    url = ("https://www.googleapis.com/upload/drive/v3/files"
           "?uploadType=multipart")
    resp = _requests.post(
        url,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type":
                     "multipart/related; boundary=%s" % boundary},
        data=body, timeout=300)
    if resp.status_code not in (200, 201):
        raise RenderError("Drive upload failed for %s: HTTP %d %s"
                          % (name, resp.status_code, resp.text[:300]))
    file_id = resp.json().get("id")
    if not file_id:
        raise RenderError("Drive upload returned no file id for %s" % name)
    return file_id


def fetch_media(items, tmpdir, prefix, ext):
    """Write a list of media items to disk, return list of file paths
    (None entries stay None — ambience silence slots)."""
    return [_fetch_one(item, tmpdir, prefix, i, ext)
            for i, item in enumerate(items)]


def fetch_single(item, tmpdir, name, ext):
    """Resolve one single item (narration, video). Raises RenderError
    when the item is missing."""
    if item is None:
        raise RenderError("no %s supplied" % name)
    path = _fetch_one(item, tmpdir, name, 0, ext)
    if path is None:
        raise RenderError("no %s supplied" % name)
    return path


# ---------------------------------------------------------------------------
# Caption measurement (ported from voice/measure_captions.py) and karaoke
# ASS generation (ported from demo/style-test/words_to_ass.py).
# ---------------------------------------------------------------------------

def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _fmt_srt(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _fmt_ass(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%d:%02d:%02d.%02d" % (h, m, s, ms // 10)


def _clean_word(t):
    return t.upper().replace("'", "").replace("{", "").replace("}", "")


def chunk_phrase(ph):
    """Chunk one comma-free phrase into 2-4 word cues (never 1-word
    unless the phrase itself is a single word)."""
    if len(ph) <= 4:
        return [ph]
    out, i, n = [], 0, len(ph)
    while n - i > 4:
        rem = n - i
        if rem in (5, 6):
            take = 3
        elif rem == 7:
            take = 4
        else:
            take = 4 if rem - 4 >= 2 else 3
        out.append(ph[i:i + take])
        i += take
    out.append(ph[i:])
    if len(out) > 1 and len(out[-1]) == 1 and len(out[-2]) < 4:
        out[-2] = out[-2] + out[-1]
        out.pop()
    return out


def chunk_cues(aligned):
    """Caption chunking v3: split into sentences at . ? ! (a cue NEVER
    crosses a sentence boundary), split each sentence into phrases at
    commas, then chunk each phrase into 2-4 word cues. Punctuation
    therefore always ends a cue, never starts one or sits mid-cue."""
    sentences, cur = [], []
    for a in aligned:
        cur.append(a)
        if a["text"] and a["text"][-1] in ".?!":
            sentences.append(cur)
            cur = []
    if cur:
        sentences.append(cur)
    cues = []
    for sent in sentences:
        phrases, ph = [], []
        for a in sent:
            ph.append(a)
            if a["text"].endswith(","):
                phrases.append(ph)
                ph = []
        if ph:
            phrases.append(ph)
        for ph in phrases:
            cues.extend(chunk_phrase(ph))
    return cues


def align_words(script, whisper_words):
    """Align the script words to Whisper's measured word onsets.
    Caption text always uses the SCRIPT words at Whisper's measured
    positions: if Whisper mishears a word (e.g. "once" -> "one"), the
    script word is kept at that word's spoken time. Hyphen splits
    (self + doubt) are merged. Never uniform per-word timing."""
    sw = [w for w in script.split() if _norm(w)]
    aligned, j = [], 0
    for s in sw:
        target, start, end, acc, match = _norm(s), None, None, "", False
        while j < len(whisper_words):
            w = whisper_words[j]
            n = _norm(w["word"])
            if start is None:
                start = w["start"]
                end = w["end"]
            # Peek before consuming: only take the word if the accumulated
            # letters can still become the target. (Old code consumed first
            # and checked after, so "Are" heard as "A" swallowed the next
            # word and shifted the following words by one position.)
            if acc + n == target:
                end = w["end"]
                j += 1
                match = True
                break
            if target.startswith(acc + n):
                end = w["end"]
                j += 1
                acc += n
                continue  # hyphen split, e.g. self + doubt
            break  # mishearing: accept position timing, do not consume
        aligned.append({"text": s.strip("'"), "start": start,
                        "end": end, "match": match})
    # Sanitise timings: Whisper sometimes gives a word a zero-length span,
    # which would make its caption line invisible. Give such a word a short
    # 0.12s slice and push any word that starts inside it to just after
    # it, so every word gets a visible slice and starts stay strictly
    # increasing.
    prev_end = 0.0
    for a in aligned:
        if a["start"] is not None and a["start"] < prev_end:
            a["start"] = prev_end
        if a["end"] is None or a["end"] <= a["start"]:
            a["end"] = (a["start"] or 0.0) + 0.12
        prev_end = a["end"]
    if any(a["start"] is None for a in aligned):
        missing = [a["text"] for a in aligned if a["start"] is None]
        raise RenderError("words with no timing: %s" % missing)
    return aligned


def measure_captions(audio_path, script, tmpdir):
    """Run faster-whisper (tiny, int8, CPU) with word timestamps on the
    narration, align the measured onsets to the script words, and return
    (aligned_words, stats, srt_text)."""
    import numpy as np
    from faster_whisper import WhisperModel

    raw_path = os.path.join(tmpdir, "audio-16k.raw")
    rc, _out, err = run([
        "ffmpeg", "-y", "-v", "error", "-i", audio_path, "-ar", "16000",
        "-ac", "1", "-f", "f32le", "-acodec", "pcm_f32le", raw_path,
    ], timeout=WHISPER_TIMEOUT)
    if rc != 0:
        raise RenderError("audio decode for caption measurement failed: %s"
                          % err[-1000:])
    pcm = np.fromfile(raw_path, dtype=np.float32)
    model = WhisperModel("tiny", device="cpu", compute_type="int8",
                         cpu_threads=2)
    segments, _info = model.transcribe(pcm, language="en",
                                       word_timestamps=True, beam_size=1)
    ww = []
    for seg in segments:
        for w in (seg.words or []):
            ww.append({"word": w.word.strip(), "start": round(w.start, 2),
                       "end": round(w.end, 2)})
    aligned = align_words(script, ww)
    cues = chunk_cues(aligned)
    out = []
    for i, cue in enumerate(cues):
        st = cue[0]["start"]
        en = cue[-1]["end"]
        txt = " ".join(w["text"] for w in cue).upper().replace("'", "")
        out.append("%d\n%s --> %s\n%s\n" % (i + 1, _fmt_srt(st),
                                            _fmt_srt(en), txt))
    bad = [a["text"] for a in aligned if not a["match"]]
    stats = {
        "words": len(aligned),
        "exact": len(aligned) - len(bad),
        "position_only": bad,
        "cues": len(cues),
        "duration": round(aligned[-1]["end"], 2),
    }
    return aligned, stats, "\n".join(out)


def karaoke_ass(aligned):
    """Build a karaoke-highlight ASS file from aligned word timings.
    Each 2-4 word cue becomes one dialogue line per word: the word being
    spoken is bright gold, the other words are dim bronze-gold, so the eye
    follows the voice. Each line runs to the next word's onset so the
    caption never blinks out mid-cue. NO fade anywhere."""
    lines = [ASS_HEADER]
    n = 0
    cues = chunk_cues(aligned)
    for cue in cues:
        words = [_clean_word(w["text"]) for w in cue]
        for k, w in enumerate(cue):
            st = w["start"]
            # run to the next word's onset so the caption never blinks out
            en = cue[k + 1]["start"] if k + 1 < len(cue) else w["end"]
            text = " ".join((GOLD if m == k else DIM) + wd
                            for m, wd in enumerate(words))
            lines.append("Dialogue: 0,%s,%s,Default,,0,0,0,,%s%s"
                         % (_fmt_ass(st), _fmt_ass(en), PRE, text))
            n += 1
    return "\n".join(lines) + "\n", n, len(cues)


def burn_captions(video_path, ass_path, out_path):
    """Burn an ASS file into a video (bridge /captions). Re-encodes the
    video, copies the audio. The ASS file carries the locked style, so
    no force_style is used (ffmpeg's subtitles filter ignores Alignment
    on this build)."""
    esc = ass_path.replace("\\", "\\\\").replace(":", "\\:") \
        .replace("'", "\\'")
    rc, _out, err = run([
        "ffmpeg", "-y", "-threads", "2", "-i", video_path,
        "-vf", "subtitles='%s'" % esc,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "copy", "-movflags", "+faststart", out_path,
    ], timeout=FFMPEG_TIMEOUT)
    if rc != 0:
        raise RenderError("caption burn failed: %s" % err[-1000:])


# ---------------------------------------------------------------------------
# Assemble (ported from bridge._assemble).
# ---------------------------------------------------------------------------

def assemble(manifest, tmpdir, out_path):
    """Build the 1080x1920 MP4. Returns a summary dict.
    Raises RenderError on any failure (code 2 = duration outside 30-60s)."""
    job_id = str(manifest.get("job_id") or "job")
    job_id = re.sub(r"[^A-Za-z0-9_-]", "_", job_id)[:64]
    media = manifest.get("media") or []
    audio_item = manifest.get("narration") or manifest.get("audio")
    srt_text = manifest.get("srt_text") or ""
    ass_text = manifest.get("ass_text") or ""
    ambience = manifest.get("ambience") or []

    if not media:
        raise RenderError("no media supplied")
    if audio_item is None:
        raise RenderError("no narration supplied")

    # 1. Inputs to disk.
    try:
        media_paths = fetch_media(media, tmpdir, "media", "bin")
        # Probe each item: video stream -> clip, otherwise still.
        # A still image probes as a 1-frame "video" of 0.04s, so
        # duration is the real test: <=0.5s means still. (A still
        # misclassified as a clip hangs ffmpeg forever: -stream_loop
        # restarts its timestamps at 0 each loop, so trim=0:<seg>
        # never sees a timestamp past the cut and never gets EOF.)
        kinds = []
        for path in media_paths:
            if path is None:
                raise RenderError("media item resolved to nothing")
            w, _h, d = probe_video(path)
            kinds.append("clip" if (w is not None and d is not None
                                    and d > 0.5) else "image")
    except RenderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RenderError("bad media input: %s" % exc)
    try:
        audio_path = fetch_single(audio_item, tmpdir, "audio", "mp3")
    except RenderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RenderError("bad narration input: %s" % exc)

    # 2. Audio duration drives the video length; reject early if out of range.
    audio_dur = probe_duration(audio_path)
    if audio_dur is None:
        raise RenderError("could not read audio duration")
    if audio_dur < 30 or audio_dur > 60:
        raise RenderError("audio duration %.2fs outside 30-60s range"
                          % audio_dur, code=2)

    n = len(media_paths)
    # With crossfades, each segment's share is longer than its final
    # on-screen span: n*seg - (n-1)*XFADE == audio_dur.
    seg = ((audio_dur + (n - 1) * XFADE) / n) if n > 1 else audio_dur

    # Ambience: one natural ambient sound per scene, fetched like
    # media (URL/base64/local path). A null or missing entry means
    # silence for that segment. Fetch failures are a hard error — a
    # video must never ship with the wrong scene's sound.
    amb_paths = []
    if ambience:
        try:
            amb_paths = fetch_media(ambience, tmpdir, "amb", "mp3")
        except RenderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RenderError("bad ambience input: %s" % exc)
        amb_paths = (amb_paths + [None] * n)[:n]

    # 3. Captions to disk as a real ASS file (ALL CAPS dialogue, locked
    # middle-centre style), if supplied. A caller-supplied ASS (with
    # per-word highlight) wins over the SRT conversion.
    ass_path = None
    if ass_text and ass_text.strip():
        ass_path = os.path.join(tmpdir, "subs.ass")
        with open(ass_path, "w") as fh:
            fh.write(ass_text)
    elif srt_text.strip():
        ass_path = os.path.join(tmpdir, "subs.ass")
        with open(ass_path, "w") as fh:
            fh.write(srt_to_ass(upper_srt(srt_text)))

    # 4. Normalise clips first, one at a time: mixed 4K/mixed-fps
    # inputs in one filtergraph OOM-kill the encode (died at ~18.5s
    # output both times, RC -9). Each clip is transcoded alone to
    # uniform 1080x1920 / 30fps / SAR 1, taking only its share
    # (the assembly only ever uses the first <seg> seconds; a
    # shorter clip keeps its full length and is looped later).
    # Stills skip this: they are already small and get zoompan.
    for i, path in enumerate(media_paths):
        if kinds[i] != "clip":
            continue
        norm = os.path.join(tmpdir, "norm%d.mp4" % i)
        rc, _o, err = run([
            "ffmpeg", "-y", "-threads", "2",
            "-t", "%.3f" % seg, "-i", path,
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,"
                   "crop=1080:1920,setsar=1,fps=30,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-an", norm,
        ], timeout=300)
        if rc != 0:
            raise RenderError("clip normalise failed (item %d): %s"
                              % (i, err[-1000:]))
        media_paths[i] = norm

    # 5. Build the video: each input gets an equal share of the audio,
    # full-bleed 1080x1920 (scale+crop fills the frame, never letterbox).
    # Clips are real moving video, trimmed to their share (looped first
    # so a short clip still fills its share). Images get a slow push-in
    # (zoompan 1.00 -> 1.10) so the frame moves.
    inputs = []
    parts = []
    for i, path in enumerate(media_paths):
        if kinds[i] == "clip":
            inputs += ["-stream_loop", "-1", "-t", "%.3f" % seg,
                       "-i", path]
            parts.append(
                "[%d:v]trim=0:%.3f,setpts=PTS-STARTPTS,"
                "scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,setsar=1,format=yuv420p,settb=AVTB[v%d]"
                % (i, seg, i))
        else:
            inputs += ["-framerate", "30", "-loop", "1",
                       "-t", "%.3f" % seg, "-i", path]
            frames = max(1, int(round(seg * 30)))
            parts.append(
                "[%d:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,"
                "zoompan=z='1+0.10*on/%d':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                "d=1:s=1080x1920:fps=30,setsar=1,format=yuv420p,settb=AVTB[v%d]"
                % (i, frames, i))
    inputs += ["-i", audio_path]

    # Ambience audio inputs, one per segment, trimmed to the segment's
    # OUTPUT span so the sound changes exactly when the scene does
    # (first n-1 pieces are seg-XFADE long, the last is seg long).
    # Input indices: media 0..n-1, narration n, ambience n+1...
    # The change from one scene's sound to the next is a 1.0s
    # CROSSFADE (acrossfade), never a fade-out to silence and back:
    # each piece is fed ACROSS seconds longer than its span, and the
    # crossfade blends the two sounds while the picture dissolves,
    # so the change is smooth, refreshing and relaxing with no dip
    # to silence. Every piece is resampled to 44100 Hz stereo so
    # acrossfade and the final mix always see matching formats.
    amb_filters, amb_labels = [], []
    if amb_paths:
        piece_lens = ([seg - XFADE] * (n - 1) + [seg] if n > 1
                      else [audio_dur])
        for i in range(n):
            plen = piece_lens[i]
            fed = plen + ACROSS if i < n - 1 else plen
            src = n + 1 + i
            if amb_paths[i]:
                inputs += ["-stream_loop", "-1", "-t", "%.3f" % fed,
                           "-i", amb_paths[i]]
                filt = ("[%d:a]volume=0.2,aresample=44100,"
                        "aformat=channel_layouts=stereo" % src)
                if i == 0:
                    filt += ",afade=t=in:st=0:d=0.5"
                if i == n - 1:
                    filt += (",afade=t=out:st=%.3f:d=0.5"
                             % max(0.0, fed - 0.5))
                amb_filters.append(filt + "[amb%d]" % i)
                amb_labels.append("[amb%d]" % i)
            else:
                inputs += ["-f", "lavfi", "-t", "%.3f" % fed,
                           "-i", "anullsrc=r=44100:cl=stereo"]
                amb_labels.append("[%d:a]" % src)

    # Scene changes use a gentle 0.5s cross-dissolve (xfade=fade),
    # never a hard cut — comfortable to the eyes. Each xfade step's
    # offset is relative to ITS first input (the chain accumulated
    # so far), NOT the final output timeline: the k-th join starts
    # 0.5s before the accumulated chain ends, i.e. k*(seg-XFADE).
    # (The old formula (k+1)*seg-(k+1)*XFADE was one segment too
    # far, so xfade saw an offset past its first input's end and
    # the output collapsed to a single segment, ~6.5s.)
    if n > 1:
        xparts = []
        prev = "[v0]"
        for k in range(1, n):
            off = k * (seg - XFADE)
            xparts.append(
                "%s[v%d]xfade=transition=fade:duration=%.3f:"
                "offset=%.3f[x%d]" % (prev, k, XFADE, off, k))
            prev = "[x%d]" % k
        video_chain = ";".join(parts + xparts) + ";" + prev
    else:
        video_chain = ";".join(parts) + ";[v0]"
    vf = (video_chain +
          "fade=t=in:st=0:d=0.5,"
          "fade=t=out:st=%.3f:d=0.5,"
          "vignette=PI/4" % (audio_dur - 0.5))
    burned_in = False
    if ass_path and find_font():
        # Escape for the subtitles filter: backslash, colon, quote.
        esc = ass_path.replace("\\", "\\\\").replace(":", "\\:") \
            .replace("'", "\\'")
        vf += ",subtitles='%s'" % esc
        burned_in = True
    elif ass_path:
        # Same as the bridge: no usable font means the video is
        # produced without burn-in rather than failing the job.
        log("WARNING: no caption font found — producing video without "
            "burned-in captions")
    vf += "[v]"

    # Mix the narration with the ambience track, keeping the voice
    # dominant: ambience is already at 0.2, and normalize=0 so the
    # mix does not rescale the narration down. The narration is
    # resampled to 44100 Hz stereo first so amix sees matching
    # formats (it is 24 kHz mono from Kokoro).
    if amb_paths and any(amb_paths):
        parts_audio = list(amb_filters)
        if n > 1:
            prev = amb_labels[0]
            for k in range(1, n):
                parts_audio.append(
                    "%s%sacrossfade=d=%.3f:c1=tri:c2=tri[ax%d]"
                    % (prev, amb_labels[k], ACROSS, k))
                prev = "[ax%d]" % k
            amb_track = prev
        else:
            amb_track = amb_labels[0]
        parts_audio.append(
            "[%d:a]aresample=44100,aformat=channel_layouts=stereo[nar]"
            % n)
        parts_audio.append(
            "[nar]%samix=inputs=2:duration=first:"
            "dropout_transition=0:normalize=0[aout]" % amb_track)
        vf += ";" + ";".join(parts_audio)
        audio_map = "[aout]"
    else:
        audio_map = "%d:a" % n

    cmd = (["ffmpeg", "-y", "-threads", "2"] + inputs + [
        "-filter_complex", vf,
        "-map", "[v]",
        "-map", audio_map,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-shortest", out_path,
    ])
    rc, _out, err = run(cmd, timeout=FFMPEG_TIMEOUT)
    if rc != 0:
        raise RenderError("ffmpeg failed: %s" % err[-2000:])

    # 6. Measure the finished MP4; reject if outside 30-60s.
    width, height, duration = probe_video(out_path)
    if duration is None:
        raise RenderError("could not measure output video")
    if duration < 30 or duration > 60:
        raise RenderError("output duration %.2fs outside 30-60s range"
                          % duration, code=2)
    return {
        "job_id": job_id,
        "output": out_path,
        "duration": round(duration, 3),
        "width": width,
        "height": height,
        "burned_in": burned_in,
    }


# ---------------------------------------------------------------------------
# Captions mode (bridge /measure-captions + /captions in one run).
# ---------------------------------------------------------------------------

def captions_mode(manifest, tmpdir, out_path):
    """Measure word timings from the narration, build the karaoke ASS
    and burn it into the video. Returns a summary dict."""
    job_id = str(manifest.get("job_id") or "job")
    job_id = re.sub(r"[^A-Za-z0-9_-]", "_", job_id)[:64]
    video_item = manifest.get("video")
    audio_item = manifest.get("narration") or manifest.get("audio")
    script = (manifest.get("script") or "").strip()

    if video_item is None:
        raise RenderError("no video supplied")
    if audio_item is None:
        raise RenderError("no narration supplied")
    if not script:
        raise RenderError("no script supplied")
    if not find_font():
        raise RenderError("no caption font found — cannot burn captions")

    video_path = fetch_single(video_item, tmpdir, "video", "mp4")
    audio_path = fetch_single(audio_item, tmpdir, "audio", "mp3")

    log("measuring caption timings with faster-whisper (tiny, int8)...")
    aligned, stats, _srt_text = measure_captions(audio_path, script, tmpdir)
    log("measured %d words (%d exact, %d position-only), %d cues"
        % (stats["words"], stats["exact"], len(stats["position_only"]),
           stats["cues"]))
    ass_text, lines, cues = karaoke_ass(aligned)
    ass_path = os.path.join(tmpdir, "subs.ass")
    with open(ass_path, "w") as fh:
        fh.write(ass_text)
    log("karaoke ASS built: %d dialogue lines, %d cues" % (lines, cues))

    log("burning captions into video...")
    burn_captions(video_path, ass_path, out_path)

    width, height, duration = probe_video(out_path)
    if duration is None:
        raise RenderError("could not measure output video")
    if duration < 30 or duration > 60:
        raise RenderError("output duration %.2fs outside 30-60s range"
                          % duration, code=2)
    summary = {
        "job_id": job_id,
        "output": out_path,
        "duration": round(duration, 3),
        "width": width,
        "height": height,
        "burned_in": True,
    }
    summary.update(stats)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_manifest(spec):
    """--manifest accepts a path to a JSON file or a JSON string."""
    if os.path.isfile(spec):
        with open(spec, encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(spec)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Equanimous render runner (GitHub Actions port of "
                    "the local FFmpeg bridge).")
    parser.add_argument("--mode", required=True,
                        choices=["assemble", "captions"],
                        help="assemble (W05) or captions (W06)")
    parser.add_argument("--manifest", required=True,
                        help="path to a manifest JSON file, or a JSON string")
    parser.add_argument("--video-id", default="",
                        help="video id, used as job_id when the manifest "
                             "has none and in the summary")
    parser.add_argument("--output", default="",
                        help="output MP4 path (overrides manifest output)")
    parser.add_argument("--drive-key", default="",
                        help="path to the Google service account JSON key "
                             "(for Drive items and Drive upload)")
    parser.add_argument("--drive-folder", default="",
                        help="Drive folder ID to upload the finished video "
                             "to (overrides manifest drive_folder)")
    args = parser.parse_args(argv)

    try:
        check_tools()
        manifest = load_manifest(args.manifest)
        if not isinstance(manifest, dict):
            raise RenderError("manifest must be a JSON object")
        if args.video_id:
            manifest.setdefault("job_id", args.video_id)
        out_path = (args.output or manifest.get("output") or
                    ("out/%s.mp4" % (args.video_id or "output")))
        parent = os.path.dirname(os.path.abspath(out_path))
        if parent:
            os.makedirs(parent, exist_ok=True)

        tmpdir = tempfile.mkdtemp(prefix="render-%s-" %
                                  (args.video_id or "job"))
        try:
            log("mode=%s video_id=%s" % (args.mode, args.video_id or "?"))
            if args.mode == "assemble":
                summary = assemble(manifest, tmpdir, out_path)
            else:
                summary = captions_mode(manifest, tmpdir, out_path)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        # Upload the finished video to Drive when a folder is given
        # (manifest drive_folder, or the --drive-folder flag). The file
        # ID goes in the summary so n8n can read it back from Drive.
        if args.drive_key:
            _drive_key_path[0] = args.drive_key
        drive_folder = (args.drive_folder or manifest.get("drive_folder")
                        or "")
        if drive_folder:
            out_name = os.path.basename(out_path)
            summary["drive_file_id"] = _drive_upload(
                out_path, out_name, drive_folder)
            log("uploaded %s to Drive folder %s as %s"
                % (out_name, drive_folder, summary["drive_file_id"]))

        summary["status"] = "ok"
        summary["mode"] = args.mode
        if args.video_id:
            summary["video_id"] = args.video_id
        # The JSON summary is the ONLY thing on stdout, so n8n can parse it.
        print(json.dumps(summary))
        return 0
    except RenderError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return exc.code
    except json.JSONDecodeError as exc:
        print("ERROR: invalid manifest JSON: %s" % exc, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
