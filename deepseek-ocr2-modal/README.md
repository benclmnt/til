# deepseek-ocr2-modal

Deploy [DeepSeek-OCR-2](https://github.com/deepseek-ai/DeepSeek-OCR-2) on [Modal](https://modal.com) using **vLLM** (`AsyncLLMEngine` + custom `DeepseekOCR2ForCausalLM`), matching the upstream `DeepSeek-OCR2-vllm` flow (not `vllm serve` / OpenAI).

**Endpoints**

- `POST /ocr` — raster images  
- `POST /ocr_pdf` — PDFs (rendered page-by-page, same engine as images)

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- Modal CLI: `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` ([settings](https://modal.com/settings))

## Deploy

```bash
cd deepseek-ocr2-modal
make deploy
```

Other targets: `make help`, `make sync`, `make run-url`.

## Call the API

The example client picks **`/ocr`** vs **`/ocr_pdf`** from the file extension:

```bash
DEEPSEEK_OCR_API_URL='https://…--deepseek-ocr2-vllm-serve.modal.run' \
  uv run python ocr_client.py page.png

DEEPSEEK_OCR_API_URL='https://…--deepseek-ocr2-vllm-serve.modal.run' \
  uv run python ocr_client.py document.pdf
```

Optional: `--dpi` (PDF), `--raw-pages`, `--prompt`, `--json`.

`/ocr_pdf` responses include `skip_repeat`, `pages_missing_eos`, and `pages_skipped` so you can
quickly see whether missing EOS markers or repeat-guard logic affected extraction.

## Hugging Face pinning (optional)

Without **`HF_MODEL_REVISION`** (or **`HF_REVISION`**), the Hub model tracks the **default branch** (typically latest on `main`). Set a **commit SHA** (or tag) to pin weights and remote code. Optional **`HF_TOKEN`** for rate limits and auth.

Full list of environment variables (GPU, memory, PDF limits, etc.) is in the docstring at the top of `deploy_deepseek_ocr2_modal.py`.

## Layout

| File | Role |
|------|------|
| `deploy_deepseek_ocr2_modal.py` | Modal image, clone upstream OCR repo, FastAPI, `/ocr` + `/ocr_pdf` |
| `ocr_client.py` | Example client for image or PDF |
| `pyproject.toml` | Local dependencies |
