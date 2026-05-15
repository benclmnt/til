# yt-parakeet-stt

Download a video's audio track and transcribe it with the NVIDIA Parakeet STT endpoint running on Modal.

Supports YouTube, Twitter/X, Spotify podcast episodes, and most sites that [yt-dlp](https://github.com/yt-dlp/yt-dlp) can handle.

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- `PARAKEET_API_URL` environment variable (or pass `--parakeet-url`)
- `ffmpeg` installed (for audio extraction)

## Usage

```bash
cd yt-parakeet-stt

# YouTube
PARAKEET_API_URL='https://…--parakeet-transcription-web.modal.run' \
  uv run transcribe.py 'https://www.youtube.com/watch?v=…'

# Twitter/X
PARAKEET_API_URL='https://…--parakeet-transcription-web.modal.run' \
  uv run transcribe.py 'https://x.com/…'

# Spotify podcast episode
PARAKEET_API_URL='https://…--parakeet-transcription-web.modal.run' \
  uv run transcribe.py 'https://open.spotify.com/episode/…'

# Or pass URL directly
uv run transcribe.py 'https://…' \
  --parakeet-url 'https://…--parakeet-transcription-web.modal.run'
```

The transcript is written to the current folder as a `.txt` file named after the first line of the transcript.

For quick debugging, you can transcribe only a short clip instead of downloading the full episode:

```bash
PARAKEET_API_URL='https://…--parakeet-transcription-web.modal.run' \
  uv run transcribe.py 'https://www.youtube.com/watch?v=…' \
  --duration 30

PARAKEET_API_URL='https://…--parakeet-transcription-web.modal.run' \
  uv run transcribe.py 'https://www.youtube.com/watch?v=…' \
  --duration 30 \
  --diarization-backend sortformer
```

### Options

| Flag | Description |
|------|-------------|
| `--parakeet-url` | Parakeet API base URL |
| `--out-dir` | Directory to write outputs (default: `.`) |
| `--json` | Also write the full JSON response alongside the transcript |
| `--keep-media` | Keep the downloaded audio file in `--out-dir` |
| `--diarization-backend` | Request `clustering` or `sortformer` |
| `--start` | Start offset for the clip, in seconds or `HH:MM:SS` |
| `--duration` | Only download/transcribe this many seconds from `--start` |

## Notes

- Spotify support is limited to public podcast episode URLs.
- Spotify music tracks/albums are not supported.
- Spotify episodes are resolved from the public web player and downloaded as `.m4a`.
- For clipped Spotify downloads (`--start`/`--duration`), `ffmpeg` is still used after download.
- If Spotify changes their web player internals, episode resolution may need updating.

## How it works

1. **Download** — For Spotify podcast episodes, the script resolves the public web-player audio URL and downloads the `.m4a` directly; if you request a clip, it trims it with `ffmpeg`. For other sites, `yt-dlp` extracts the best audio stream and converts it to `.m4a`.
2. **Resume** — Audio is cached as a dot-prefixed file in `--out-dir`. If transcription fails, re-run the same command to skip re-downloading.
3. **Transcribe** — The audio file is uploaded to the Parakeet Modal endpoint (`POST /transcribe`).
4. **Write** — The transcript (and optional JSON) is saved to disk. The cache is cleaned up on success unless `--keep-media` is passed.

`sortformer` is intended for short clips only and is limited to 120 seconds by the client.
