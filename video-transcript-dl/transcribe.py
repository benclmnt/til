# /// script
# requires-python = ">=3.11"
# dependencies = ["yt-dlp>=2025.0", "httpx>=0.27"]
# ///

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

from yt_dlp import YoutubeDL

_DIARIZATION_BACKENDS = ("clustering", "sortformer")
_SORTFORMER_MAX_AUDIO_SECONDS = 120.0

# Allow importing from sibling parakeet-modal folder
_PARAKEET_DIR = Path(__file__).resolve().parent.parent / "parakeet-modal"
sys.path.insert(0, str(_PARAKEET_DIR))
from transcribe_client import transcribe as parakeet_transcribe


def check_environment(
    api_url: str | None,
    *,
    require_parakeet: bool = True,
    require_ffmpeg: bool = True,
) -> None:
    """Fail fast if required tooling or config is missing."""
    errors: list[str] = []

    if require_parakeet and not api_url:
        errors.append(
            "Missing Parakeet API URL. Set --parakeet-url or the PARAKEET_API_URL environment variable."
        )

    if require_ffmpeg and shutil.which("ffmpeg") is None:
        errors.append(
            "ffmpeg not found in PATH. It's required to extract audio from videos.\n"
            "Install it with:  brew install ffmpeg   (macOS)\n"
            "                 sudo apt install ffmpeg (Ubuntu/Debian)"
        )

    if errors:
        for msg in errors:
            print(f"ERROR: {msg}", file=sys.stderr)
        raise SystemExit(1)


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _is_youtube(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url, re.IGNORECASE))


def download_youtube_subtitles(url: str, target_path: Path) -> Path | None:
    """Try to download English VTT subtitles from YouTube. Returns the .vtt path or None."""
    ydl_opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en.*"],
        "subtitlesformat": "vtt",
        "outtmpl": str(target_path.with_suffix("")),
        "quiet": True,
        "no_warnings": True,
    }

    with YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
        except Exception:
            return None

    if not info:
        return None

    # yt-dlp writes subs next to the output template; find the VTT that was just written
    base = target_path.with_suffix("")
    candidates = sorted(
        base.parent.glob(f"{base.name}*.vtt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def parse_vtt_to_text(vtt_path: Path, start_time: float = 0.0, duration: float | None = None) -> str:
    """Convert a WebVTT file to plain text, optionally clipping by time."""
    end_time = None if duration is None else start_time + duration
    lines: list[str] = []
    in_cue = False

    for raw_line in vtt_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.upper() == "WEBVTT" or line.startswith("NOTE"):
            in_cue = False
            continue
        # Cue timing line: 00:00:01.000 --> 00:00:05.000
        if " --> " in line:
            in_cue = True
            cue_start_str, _ = line.split(" --> ", 1)
            cue_start = _vtt_timestamp_to_seconds(cue_start_str.strip())
            if end_time is not None and cue_start >= end_time:
                # Past the clip end; we can stop (VTT cues are ordered)
                break
            if start_time > 0 and cue_start < start_time:
                in_cue = False  # skip this cue's text lines
            continue
        if in_cue:
            # Remove inline VTT tags like <c>, <b>, etc.
            text = re.sub(r"<[^>]+>", "", line)
            if text:
                lines.append(text)

    return " ".join(lines)


def _vtt_timestamp_to_seconds(ts: str) -> float:
    """Parse VTT timestamp (HH:MM:SS.mmm or MM:SS.mmm) into seconds."""
    parts = ts.replace(",", ".").split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    else:
        return float(parts[0])


def parse_time_value(value: str) -> float:
    """Parse seconds or HH:MM:SS[.ms]-style timestamps into seconds."""
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("time value cannot be empty")

    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)

    parts = text.split(":")
    if len(parts) not in {2, 3}:
        raise argparse.ArgumentTypeError(
            f"invalid time value {value!r}; use seconds or HH:MM:SS"
        )

    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid time value {value!r}; use seconds or HH:MM:SS"
        ) from exc

    total = 0.0
    for part in numbers:
        total = total * 60 + part
    return total


def clip_label(start_time: float, duration: float | None) -> str:
    if start_time == 0 and duration is None:
        return "full"

    start_label = f"{start_time:g}s"
    if duration is None:
        return f"from_{start_label}"
    return f"from_{start_label}_for_{duration:g}s"


def download_audio(
    url: str,
    target_path: Path,
    *,
    start_time: float = 0.0,
    duration: float | None = None,
) -> Path:
    """Download video audio to target_path using yt-dlp. Returns the final audio file path."""
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(target_path.with_suffix("")),
        "quiet": False,
        "no_warnings": False,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "192",
            }
        ],
    }

    if start_time > 0 or duration is not None:
        end_time = None if duration is None else start_time + duration

        def download_ranges(_info_dict: dict, _ydl: YoutubeDL) -> list[dict[str, float]]:
            section: dict[str, float] = {"start_time": start_time}
            if end_time is not None:
                section["end_time"] = end_time
            return [section]

        ydl_opts["download_ranges"] = download_ranges

        # Rough cuts are good enough for quick API debugging, and skipping
        # force_keyframes_at_cuts avoids the extra re-encode work.

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise SystemExit("Failed to extract video info")

    m4a_path = target_path.with_suffix(".m4a")
    if m4a_path.exists():
        return m4a_path

    candidates = list(target_path.parent.glob(f"{target_path.stem}.*"))
    if candidates:
        return candidates[0]

    raise SystemExit(f"Downloaded audio not found at {target_path}*")


def sanitize_filename(text: str, max_len: int = 100) -> str:
    """Create a safe filename from arbitrary text."""
    text = re.sub(r"[^\w\s-]", "", text).strip().replace(" ", "_")
    return text[:max_len] if text else "transcript"


def summarize_diarization(result: dict) -> tuple[int, int, int]:
    speakers = result.get("speakers") or []
    utterances = result.get("utterances") or []
    speaker_segments = result.get("speaker_segments") or []
    return len(speakers), len(utterances), len(speaker_segments)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transcribe a video via YouTube subtitles when available, otherwise fall back to Parakeet STT."
    )
    parser.add_argument("url", help="Video URL (YouTube, Twitter/X, etc. — any site yt-dlp supports)")
    parser.add_argument(
        "--parakeet-url",
        default=os.environ.get("PARAKEET_API_URL"),
        help="Parakeet API base URL for non-YouTube or subtitle-fallback transcription (or set PARAKEET_API_URL env var)",
    )
    parser.add_argument(
        "--out-dir",
        default=".",
        type=Path,
        help="Directory to write the transcript (default: current folder)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also write full JSON response alongside the transcript",
    )
    parser.add_argument(
        "--keep-media",
        action="store_true",
        help="Keep downloaded audio/subtitle cache files instead of cleaning them up",
    )
    parser.add_argument(
        "--diarize",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable multi-speaker diarization",
    )
    parser.add_argument(
        "--single-speaker",
        action="store_true",
        help="For YouTube URLs, use yt-dlp subtitles instead of audio transcription/diarization",
    )
    parser.add_argument(
        "--diarization-backend",
        choices=_DIARIZATION_BACKENDS,
        default=None,
        help="Diarization backend to request from the server",
    )
    parser.add_argument(
        "--start",
        type=parse_time_value,
        default=0.0,
        help="Clip start offset in seconds or HH:MM:SS (default: 0)",
    )
    parser.add_argument(
        "--duration",
        type=parse_time_value,
        help="Only download/transcribe this many seconds from --start",
    )
    args = parser.parse_args()

    if args.start < 0:
        parser.error("--start must be >= 0")
    if args.duration is not None and args.duration <= 0:
        parser.error("--duration must be > 0")

    is_youtube = _is_youtube(args.url)

    if (
        args.diarize
        and args.diarization_backend == "sortformer"
        and args.duration is not None
        and args.duration > _SORTFORMER_MAX_AUDIO_SECONDS
    ):
        parser.error(
            f"--diarization-backend sortformer is limited to {_SORTFORMER_MAX_AUDIO_SECONDS:.0f}s clips; "
            "use clustering for longer audio"
        )
    if args.single_speaker and not is_youtube:
        parser.error("--single-speaker is only supported for YouTube URLs")
    if args.single_speaker and not args.diarize:
        parser.error("--single-speaker cannot be combined with --no-diarize")

    check_environment(
        args.parakeet_url,
        require_parakeet=not (is_youtube and args.single_speaker),
        require_ffmpeg=not (is_youtube and args.single_speaker),
    )

    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Deterministic temp paths in out_dir so we can resume after failures
    range_key = clip_label(args.start, args.duration)
    cache_key = _url_hash(f'{args.url}|{range_key}')
    audio_path = out_dir / f".stt_{cache_key}.m4a"
    subtitle_stub = out_dir / f".stt_{cache_key}.vtt"
    subtitle_path: Path | None = None
    used_youtube_subtitles = False
    transcript = ""
    utterances: list[dict] = []
    result: dict = {}
    success = False

    try:
        if is_youtube and args.single_speaker:
            print("[1/3] Trying YouTube subtitles via yt-dlp ...", file=sys.stderr)
            subtitle_path = download_youtube_subtitles(args.url, subtitle_stub)
            if subtitle_path is not None:
                transcript = parse_vtt_to_text(
                    subtitle_path,
                    start_time=args.start,
                    duration=args.duration,
                ).strip()
                if transcript:
                    used_youtube_subtitles = True
                    result = {
                        "transcript": transcript,
                        "metadata": {
                            "source": "youtube_subtitles",
                            "subtitle_file": str(subtitle_path),
                        },
                    }
                    print("[2/3] Using YouTube subtitles.", file=sys.stderr)
                else:
                    raise RuntimeError("Downloaded YouTube subtitles, but transcript was empty")
            else:
                raise RuntimeError("No YouTube English subtitles found")

        if not used_youtube_subtitles:
            check_environment(args.parakeet_url)

            if audio_path.exists():
                print(f"[1/3] Using cached audio: {audio_path}", file=sys.stderr)
            else:
                clip_desc = (
                    f" (start={args.start:g}s, duration={args.duration:g}s)"
                    if args.duration is not None
                    else (f" (start={args.start:g}s)" if args.start > 0 else "")
                )
                print(
                    f"[1/3] Downloading audio from {args.url}{clip_desc} ...",
                    file=sys.stderr,
                )
                audio_path = download_audio(
                    args.url,
                    audio_path,
                    start_time=args.start,
                    duration=args.duration,
                )
                print(f"      Saved to {audio_path}", file=sys.stderr)

            print("[2/3] Transcribing with Parakeet ...", file=sys.stderr)
            result = parakeet_transcribe(
                audio_path,
                args.parakeet_url,
                diarize=args.diarize,
                diarization_backend=args.diarization_backend,
            )

            transcript = result.get("transcript", "")
            utterances = result.get("utterances", [])

            if args.diarize:
                speaker_count, utterance_count, segment_count = summarize_diarization(result)
                backend = (result.get("metadata") or {}).get("diarization_backend")
                if backend:
                    print(f"      Diarization backend: {backend}", file=sys.stderr)
                print(
                    "      Diarization result: "
                    f"speakers={speaker_count}, utterances={utterance_count}, speaker_segments={segment_count}",
                    file=sys.stderr,
                )
                if not utterances:
                    metadata = result.get("metadata") or {}
                    raise RuntimeError(
                        "Diarization was requested, but the API returned no utterances. "
                        f"speakers={speaker_count}, speaker_segments={segment_count}, "
                        f"metadata={json.dumps(metadata, ensure_ascii=False)}"
                    )

        print("[3/3] Writing transcript ...", file=sys.stderr)

        first_line = transcript.strip().splitlines()[0] if transcript.strip() else ""
        base_name = sanitize_filename(first_line) if first_line else "transcript"
        txt_path = out_dir / f"{base_name}.txt"
        json_path = out_dir / f"{base_name}.json"

        counter = 1
        while txt_path.exists():
            txt_path = out_dir / f"{base_name}_{counter}.txt"
            json_path = out_dir / f"{base_name}_{counter}.json"
            counter += 1

        if args.diarize and not used_youtube_subtitles:
            lines: list[str] = []
            for u in utterances:
                speaker = u.get("speaker") or "UNKNOWN"
                start = float(u.get("start", 0.0))
                end = float(u.get("end", 0.0))
                text = u.get("text", "")
                lines.append(f"[{start:8.2f} - {end:8.2f}] {speaker}: {text}")
            txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            txt_path.write_text(transcript + ("\n" if transcript and not transcript.endswith("\n") else ""), encoding="utf-8")

        print(f"      Transcript: {txt_path}", file=sys.stderr)

        if args.json:
            json_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"      JSON:       {json_path}", file=sys.stderr)

        print(txt_path.read_text(encoding="utf-8"), end="")
        success = True

    finally:
        if success and not args.keep_media and audio_path.exists():
            audio_path.unlink()
            print("      Cleaned up cached audio", file=sys.stderr)
        if success and not args.keep_media and subtitle_path is not None and subtitle_path.exists():
            subtitle_path.unlink()
            print("      Cleaned up cached subtitles", file=sys.stderr)


if __name__ == "__main__":
    main()
