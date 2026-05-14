# parakeet-modal

Deploy NVIDIA Parakeet on [Modal](https://modal.com), following the general structure of [modal-projects/modal-nvidia-asr](https://github.com/modal-projects/modal-nvidia-asr) with a simpler batch HTTP API.

Default model: `nvidia/parakeet-tdt-0.6b-v3`

This version is optimized for long batch files: audio is converted to mono 16 kHz PCM, split into fixed-size chunks, transcribed in GPU batches, then merged back into one response.

Optional multi-speaker diarization can use NeMo clustering or offline Sortformer, then aligns Parakeet word timestamps back to speaker turns.

## Endpoints

- `GET /health`
- `POST /transcribe`

`/transcribe` accepts multipart form-data with a required `file` field.

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- Modal CLI configured with `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`

## Deploy

```bash
cd parakeet-modal
uv sync
uv run modal deploy deploy_parakeet_modal.py
```

Print the deployed URL:

```bash
uv run modal run deploy_parakeet_modal.py
```

## Call the API

```bash
PARAKEET_API_URL='https://…--parakeet-transcription-web.modal.run' \
  uv run python transcribe_client.py sample.wav
```

Word timestamps:

```bash
PARAKEET_API_URL='https://…--parakeet-transcription-web.modal.run' \
  uv run python transcribe_client.py sample.wav --timestamp-level word --json
```

Speaker diarization:

```bash
PARAKEET_API_URL='https://…--parakeet-transcription-web.modal.run' \
  uv run python transcribe_client.py sample.wav --diarize --json
```

Pick a diarization backend per request:

```bash
PARAKEET_API_URL='https://…--parakeet-transcription-web.modal.run' \
  uv run python transcribe_client.py sample.wav \
  --diarize \
  --diarization-backend sortformer \
  --json
```

## Request fields

- `file` — required audio file
- `timestamp_level` — optional: `word`, `segment`, or `char`
- `return_word_confidence` — optional bool, default `true`
- `diarize` — optional bool, default `false`
- `diarization_backend` — optional: `clustering` or `sortformer`

Response includes:

- `transcript` — merged transcript across all chunks
- `word_level_info` — merged timestamps with chunk offsets applied
- `chunks` — per-chunk results and offsets

When `diarize=true`, response also includes:

- `speaker_segments` — diarizer output as speaker turns across the full file
- `words` — word timestamps with aligned speaker labels
- `utterances` — merged speaker-attributed text spans
- `speakers` — discovered speaker labels

## Environment variables

- `PARAKEET_MODEL` — default `nvidia/parakeet-tdt-0.6b-v3`
- `MAX_AUDIO_SECONDS` — default `21600` (6 hours)
- `CHUNK_SECONDS` — default `300` (5 minutes)
- `BATCH_SIZE` — default `1`
- `HF_TOKEN` — optional HF token
- `DIARIZATION_BACKEND` — default `clustering`
- `DIARIZATION_SORTFORMER_MODEL` — default `nvidia/diar_sortformer_4spk-v1`
- `DIARIZATION_MODEL` — legacy alias for `DIARIZATION_SORTFORMER_MODEL`
- `DIARIZATION_VAD_MODEL` — default `vad_multilingual_marblenet`
- `DIARIZATION_SPEAKER_MODEL` — default `titanet_large`
- `MAX_SORTFORMER_AUDIO_SECONDS` — default `120`
- `MAX_DIARIZATION_SECONDS` — default `MAX_AUDIO_SECONDS`
- `WARMUP_ON_START` — default `true`

If model download requires Hugging Face auth, set `HF_TOKEN` before deploying.

## Notes

- Audio is converted to mono 16 kHz PCM before transcription.
- `soundfile` is tried first; `ffmpeg` is used as a fallback for formats like mp3/m4a/webm.
- Long files are chunked and processed batch-by-batch on the GPU.
- `clustering` is the safest long-form backend.
- `sortformer` is for short-form audio only and is client/server-limited to 120 seconds by default.
- `diarize=true` currently requires `timestamp_level` to be omitted or set to `word`.
- This project implements batch HTTP transcription only, not streaming WebSocket transcription.
