# Equanimous render

GitHub Actions workflow that renders an Equanimous Short on a free
GitHub-hosted runner. It is the port of the local FFmpeg bridge
(`bridge/bridge.py` in the workspace) — same assembly and caption logic,
same locked rules — because the bridge's public tunnel URL is dead and
n8n Cloud cannot reach the local bridge.

## Workflow

`.github/workflows/render.yml` — triggered by `workflow_dispatch` only
(n8n calls the GitHub API; nothing runs on push). Inputs:

| Input | Description |
| --- | --- |
| `mode` | `assemble` (n8n W05) or `captions` (n8n W06) |
| `video_id` | e.g. `EQ-1794202623342-2` — also the artifact name |
| `manifest_json` | JSON string carrying the media list, narration, script and ambience for this run |

The runner installs ffmpeg, the Montserrat ExtraBold font and
faster-whisper, runs `render.py` in the requested mode, and uploads the
finished MP4 as a workflow artifact named after the `video_id`
(artifact also contains `render-summary.json` with duration, size and
caption-measurement stats). Artifact retention is 7 days.

## Modes

### assemble (W05)

Builds a 1080x1920 h264+aac MP4 from an ordered media list, a narration
audio file and an optional per-scene ambience list.

```json
{
  "job_id": "EQ-1794202623342-2",
  "media": [
    {"url": "https://cdn.pixabay.com/video/...mp4"},
    {"url": "https://cdn.pixabay.com/video/...mp4"}
  ],
  "narration": {"url": "https://.../narration.mp3"},
  "ambience": [
    {"url": "https://cdn.freesound.org/...mp3"},
    null
  ],
  "ass_text": "...karaoke ASS from W06, optional..."
}
```

A media item is `{"url": ...}`, `{"path": ...}` (runner-local file),
`{"data": "<base64>", "ext": "mp4"}`, a bare base64 string / local
path, or `{"drive": "<file-id>", "ext": "mp4"}` — a Google Drive file
(the runner reads it with the service account key; see Drive
integration below). `ambience` is one item per scene in the same order;
`null` means silence for that scene. `ass_text` (karaoke ASS) wins over
`srt_text` (plain SRT) when both are given.

### captions (W06)

Measures word timings from the narration with faster-whisper, builds the
karaoke ASS and burns it into a video.

```json
{
  "job_id": "EQ-1794202623342-2",
  "video": {"url": "https://.../assembled.mp4"},
  "narration": {"url": "https://.../narration.mp3"},
  "script": "Do bad thoughts hold you back? I know how that feels. ..."
}
```

## Locked rules (ported verbatim from the bridge)

- Output is always 1080x1920, H.264 + AAC, and must be 30–60 seconds
  long. Anything outside that range fails the run (exit code 2 — the
  bridge's HTTP 422).
- Scene changes use a 0.5s cross-dissolve (xfade), never a hard cut;
  chained-xfade offset is `k*(seg - XFADE)`. Fade in/out 0.5s, vignette.
- A media item is a clip only when its probed duration is over 0.5s;
  stills get a slow push-in (zoompan 1.00 → 1.10). 4K inputs are
  normalised alone before assembly.
- Ambience is mixed at volume 0.2 under the narration, with a 1.0s
  crossfade between scenes and `amix normalize=0` so the voice stays
  dominant. Never instrumental — natural ambient sound only.
- Captions are middle-centre, ALL CAPS, Montserrat ExtraBold 55, gold
  with a navy outline on a soft translucent dark box, karaoke highlight
  (spoken word bright gold, others dim bronze) flipping exactly on the
  measured onsets. Cues are 2–4 words, never cross a sentence boundary,
  punctuation always ends a cue. No fade anywhere.
- Limits: 200 MB total download cap, 50 MB per media item.

## Exit codes

- `0` — success; a JSON summary is printed on stdout (and saved as
  `render-summary.json` by the workflow).
- `1` — general failure (bad input, fetch failure, ffmpeg failure).
- `2` — duration outside 30–60 seconds.

## Drive integration

The runner fetches media/narration from Google Drive (by file ID) and
uploads the finished video to Drive, using a **Google service account
key stored as a GitHub Secret** (`GOOGLE_SERVICE_ACCOUNT_KEY`). The
workflow writes that secret to `service-account.json` and passes it to
`render.py` as `--drive-key`; the finished video is uploaded to the
Drive folder given by the `drive_folder` workflow input (default: the
Equanimous Contents folder) and its file ID is reported in the JSON
summary as `drive_file_id`. n8n therefore never needs to download a
GitHub artifact for the finished video — it reads the file ID from the
run summary and fetches it from Drive. The workflow still uploads the
MP4 as an artifact as a backup (7-day retention).

Setup (one-off, in the Google account that owns the Drive folders):
1. Create a Google Cloud project, enable the Google Drive API, create a
   service account and a JSON key for it. No billing, no card — free tier.
2. Share the Equanimous Drive folders with the service account email
   (Contents folder as Editor; base folder as Viewer).
3. Store the JSON key as the `GOOGLE_SERVICE_ACCOUNT_KEY` secret in the
   repo (Settings → Secrets and variables → Actions).

No API key, token or credential ever goes into this repo's code. Media
items may be `{"drive": "<file-id>", "ext": "mp4"}` in the manifest;
public URLs (Pixabay clips) still work directly without Drive.

## Local check

```sh
python3 -m py_compile render.py
python3 render.py --help
```
