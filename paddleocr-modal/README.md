# paddleocr-modal

Deploy [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) on [Modal](https://modal.com) with a GPU-backed HTTP API (`POST /ocr`).

**Input:** raster images (PNG, JPEG, etc.). PDFs are not supported here.

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- Modal CLI and credentials: `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` ([settings](https://modal.com/settings))

## Deploy

```bash
cd paddleocr-modal
make deploy
```

Other targets: `make help`, `make sync`, `make run-url` (prints the app URL and cold-starts a replica).

Equivalent without Make:

```bash
uv sync
uv run modal deploy deploy_paddleocr_modal.py
```

## Call the API

Set the deploy base URL (no trailing slash), then:

```bash
PADDLEOCR_API_URL='https://…--paddleocr-modal-gpu-serve.modal.run' \
  uv run python ocr_client.py photo.png
```

`--json` prints the raw API response.

## Layout

| File | Role |
|------|------|
| `deploy_paddleocr_modal.py` | Modal app: CUDA image, GPU function, FastAPI `/ocr` |
| `ocr_client.py` | Example HTTP client |
| `pyproject.toml` | `modal`, `httpx` for local tooling |
