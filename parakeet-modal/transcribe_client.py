from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import wave
from pathlib import Path

import httpx

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DIARIZATION_KEYS = frozenset({"speaker_segments", "words", "utterances", "speakers"})
_DIARIZATION_BACKENDS = ("clustering", "sortformer")
_SORTFORMER_MAX_AUDIO_SECONDS = 120.0


def _redirect_target(resp: httpx.Response) -> str | None:
    loc = resp.headers.get("location")
    if not loc:
        return None
    nxt = httpx.URL(loc)
    if nxt.is_relative_url:
        nxt = resp.request.url.join(nxt)
    return str(nxt)


def _post_modal_web(
    client: httpx.Client,
    url: str,
    *,
    files: dict[str, tuple[str, bytes, str]],
    data: dict[str, str],
    poll_sleep_s: float = 2.0,
    deadline_monotonic: float | None = None,
    max_redirects: int = 24,
) -> httpx.Response:
    """POST multipart once, then follow Modal's 303 chain with GET."""
    current = url
    post_pending = True
    redirects = 0

    while redirects <= max_redirects:
        while True:
            if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
                raise TimeoutError("Modal web poll exceeded total deadline.")
            try:
                if post_pending:
                    resp = client.post(current, files=files, data=data, follow_redirects=False)
                    post_pending = False
                else:
                    resp = client.get(current, follow_redirects=False)
                break
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout):
                time.sleep(poll_sleep_s)

        if resp.status_code not in _REDIRECT_STATUSES:
            return resp

        nxt = _redirect_target(resp)
        resp.read()
        if nxt is None:
            return resp
        current = nxt
        redirects += 1

    raise httpx.TooManyRedirects(
        "Exceeded max_redirects waiting for Modal web endpoint.",
        request=resp.request,
    )


def _validate_diarization_payload(payload: dict) -> None:
    missing = sorted(_DIARIZATION_KEYS.difference(payload))
    if not missing:
        return

    available = ", ".join(sorted(payload)) or "<none>"
    raise RuntimeError(
        "Diarization was requested, but the server response is missing "
        f"the diarization fields {missing}. Available top-level keys: {available}. "
        "This usually means the deployed Modal endpoint is still running an older "
        "version of deploy_parakeet_modal.py and needs to be redeployed."
    )


def _raise_for_status_with_detail(resp: httpx.Response) -> None:
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        detail = resp.text.strip()
        if not detail:
            detail = "<empty response body>"
        raise RuntimeError(
            f"Parakeet API request failed with HTTP {resp.status_code} for {resp.request.url}: {detail}"
        ) from e


def _probe_audio_duration_seconds(audio_path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is not None:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode == 0:
            try:
                return float(proc.stdout.strip())
            except ValueError:
                pass

    if audio_path.suffix.lower() == ".wav":
        with wave.open(str(audio_path), "rb") as wav_in:
            frame_rate = wav_in.getframerate()
            if frame_rate > 0:
                return wav_in.getnframes() / frame_rate

    return None


def _validate_sortformer_duration(audio_path: Path, *, diarize: bool, diarization_backend: str | None) -> None:
    if not diarize or diarization_backend != "sortformer":
        return

    duration_seconds = _probe_audio_duration_seconds(audio_path)
    if duration_seconds is None or duration_seconds <= _SORTFORMER_MAX_AUDIO_SECONDS:
        return

    raise ValueError(
        "sortformer diarization is limited to audio up to "
        f"{_SORTFORMER_MAX_AUDIO_SECONDS:.0f}s, but {audio_path} is {duration_seconds:.1f}s. "
        "Use diarization_backend='clustering' for longer audio."
    )


def transcribe(
    audio_path: Path,
    api_url: str,
    *,
    timestamp_level: str | None = None,
    return_word_confidence: bool = True,
    diarize: bool = True,
    diarization_backend: str | None = None,
    timeout: float = 1800.0,
) -> dict:
    """Send an audio file to the Parakeet Modal endpoint and return the parsed JSON response."""
    _validate_sortformer_duration(
        audio_path,
        diarize=diarize,
        diarization_backend=diarization_backend,
    )
    data_bytes = audio_path.read_bytes()
    files = {"file": (audio_path.name, data_bytes, "application/octet-stream")}
    data: dict[str, str] = {
        "return_word_confidence": str(return_word_confidence).lower(),
        "diarize": str(diarize).lower(),
    }
    if timestamp_level:
        data["timestamp_level"] = timestamp_level
    if diarization_backend:
        data["diarization_backend"] = diarization_backend

    url = api_url.rstrip("/") + "/transcribe"
    httpx_timeout = httpx.Timeout(connect=60.0, read=timeout, write=timeout, pool=30.0)
    deadline = time.monotonic() + timeout + 120.0

    with httpx.Client(timeout=httpx_timeout) as client:
        resp = _post_modal_web(
            client,
            url,
            files=files,
            data=data,
            deadline_monotonic=deadline,
        )
    _raise_for_status_with_detail(resp)
    payload = resp.json()
    if diarize:
        _validate_diarization_payload(payload)
    return payload


def format_output(payload: dict, *, diarize: bool = False, json_output: bool = False) -> str:
    """Format the API payload for stdout."""
    if json_output:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if diarize and payload.get("utterances"):
        lines: list[str] = []
        for utterance in payload["utterances"]:
            speaker = utterance.get("speaker") or "UNKNOWN"
            start = float(utterance.get("start", 0.0))
            end = float(utterance.get("end", 0.0))
            text = utterance.get("text", "")
            lines.append(f"[{start:8.2f} - {end:8.2f}] {speaker}: {text}")
        return "\n".join(lines)

    return payload.get("transcript", "")


def main() -> None:
    parser = argparse.ArgumentParser(description="Call the NVIDIA Parakeet Modal endpoint.")
    parser.add_argument("audio", help="Path to an audio file")
    parser.add_argument("--url", default=os.environ.get("PARAKEET_API_URL"), help="Base API URL")
    parser.add_argument("--timestamp-level", choices=["word", "segment", "char"], default=None)
    parser.add_argument("--return-word-confidence", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--diarize", action=argparse.BooleanOptionalAction, default=True, help="Enable multi-speaker diarization")
    parser.add_argument(
        "--diarization-backend",
        choices=_DIARIZATION_BACKENDS,
        default=None,
        help="Diarization backend to request from the server",
    )
    parser.add_argument("--json", action="store_true", help="Print full JSON response")
    args = parser.parse_args()

    if not args.url:
        raise SystemExit("Set --url or PARAKEET_API_URL")

    audio_path = Path(args.audio)
    if not audio_path.exists():
        raise SystemExit(f"File not found: {audio_path}")

    payload = transcribe(
        audio_path,
        args.url,
        timestamp_level=args.timestamp_level,
        return_word_confidence=args.return_word_confidence,
        diarize=args.diarize,
        diarization_backend=args.diarization_backend,
    )
    print(format_output(payload, diarize=args.diarize, json_output=args.json))


if __name__ == "__main__":
    main()
