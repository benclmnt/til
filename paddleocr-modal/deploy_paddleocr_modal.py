"""
Deploy PaddleOCR on Modal with a GPU-backed HTTP API.

PaddleOCR uses PaddlePaddle inference, not the vLLM engine (vLLM is for LLMs). This script follows the same *deployment
shape* as Modal's vLLM examples: CUDA base image, Modal Volume for downloaded models, GPU function, `modal deploy`.

Usage:
  cd paddleocr-modal
  uv sync
  uv run modal deploy deploy_paddleocr_modal.py

  # optional local smoke test (spins a cloud replica)
  uv run modal run deploy_paddleocr_modal.py

  # call deployed OCR (set PADDLEOCR_API_URL to your serve URL, no trailing slash)
  PADDLEOCR_API_URL=https://…--paddleocr-modal-gpu-serve.modal.run uv run python ocr_client.py photo.png
  PADDLEOCR_API_URL=… uv run python ocr_client.py doc.pdf

  ``POST /ocr`` returns JSON. ``POST /ocr_pdf`` returns ``application/zip`` (``markdown.md``,
  ``metadata.json``) — same layout as DeepSeek-OCR so ``ocr_client.py`` can unpack and print.

Environment:
  MODAL_TOKEN_ID / MODAL_TOKEN_SECRET — from https://modal.com/settings
"""

import io
import json
import os
import tempfile
import zipfile
from typing import Any

import modal


def _json_safe(value: Any) -> Any:
    """Convert PaddleOCR/numpy structures to JSON-serializable Python types."""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        np = None  # type: ignore[misc, assignment]

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if np is not None:
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, np.floating):
            return float(value)
        if isinstance(value, np.integer):
            return int(value)

    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]

    if isinstance(value, dict):
        safe_dict: dict[str, Any] = {}
        for k, v in value.items():
            safe_key = k if isinstance(k, (str, int, float, bool)) or k is None else str(k)
            safe_dict[str(safe_key)] = _json_safe(v)
        return safe_dict

    # PaddleOCR 3.x/PaddleX result objects expose a JSON-friendly `json` attribute.
    json_value = getattr(value, "json", None)
    if json_value is not None and not callable(json_value):
        return _json_safe(json_value)

    # Last-resort fallback for library-specific objects (e.g. paddlex Font).
    return str(value)


def _lines_from_paddle_result(result: Any) -> list[str]:
    """Turn PaddleOCR 2.x/3.x outputs into plain text lines (same idea as ``ocr_client.py``)."""
    if result is None:
        return []
    if isinstance(result, dict):
        result = [result]

    lines: list[str] = []
    for page in result:
        if page is None:
            continue

        if isinstance(page, dict):
            page_result = page.get("res") if isinstance(page.get("res"), dict) else page
            rec_texts = page_result.get("rec_texts")
            if isinstance(rec_texts, list):
                lines.extend(str(text) for text in rec_texts if text)
                continue

        for item in page:
            if not item or len(item) < 2:
                continue
            text_part = item[1]
            if isinstance(text_part, (list, tuple)) and text_part:
                text = str(text_part[0])
            elif isinstance(text_part, str):
                text = text_part
            else:
                text = str(text_part)
            if text:
                lines.append(text)
    return lines


def _parse_major_version(version: str) -> int:
    head = version.split(".", 1)[0]
    digits = "".join(ch for ch in head if ch.isdigit())
    return int(digits) if digits else 0

# Match Paddle's cu126 wheels; Modal supplies the NVIDIA driver on the host.
# Two steps: `paddlepaddle-gpu` from Paddle's index; everything else from PyPI so
# `paddleocr` does not get stuck on the Paddle mirror's package listing.
_PADDLE_EXTRA_INDEX = "https://www.paddlepaddle.org.cn/packages/stable/cu126/"

paddleocr_image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.6.2-cudnn-runtime-ubuntu22.04",
        add_python="3.11",
    )
    .entrypoint([])
    .apt_install(
        "libgomp1",
        "libglib2.0-0",
        "libgl1",
        "libsm6",
        "libxext6",
        "libxrender1",
    )
    .pip_install(
        "paddlepaddle-gpu==3.0.0",
        extra_index_url=_PADDLE_EXTRA_INDEX,
    )
    .pip_install(
        "paddleocr>=2.7",
        "fastapi>=0.115",
        "uvicorn[standard]>=0.30",
        "python-multipart>=0.0.9",
        "pillow>=10",
        "numpy>=1.26",
        "PyMuPDF",
    )
    .env(
        {
            "FLAGS_allocator_strategy": "auto_growth",
            # Skip PaddleX startup connectivity probes to model hosters.
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
        }
    )
)

# Default PaddleOCR download/cache location; persist across cold starts.
paddle_models_vol = modal.Volume.from_name("paddleocr-models", create_if_missing=True)

app = modal.App("paddleocr-modal-gpu")

MINUTES = 60
OCR_GPU = "T4"  # enough for PP-OCR; use A10G/H100 if you need higher throughput


@app.function(
    image=paddleocr_image,
    gpu=OCR_GPU,
    timeout=15 * MINUTES,
    scaledown_window=15 * MINUTES,
    volumes={os.path.expanduser("~/.paddleocr"): paddle_models_vol},
)
@modal.concurrent(max_inputs=8)
@modal.asgi_app()
def serve() -> Any:
    from fastapi import FastAPI, File, HTTPException, Request
    from fastapi.responses import JSONResponse, Response
    import paddleocr as paddleocr_pkg
    from paddleocr import PaddleOCR
    from PIL import Image

    # Lazy init after imports so the container can bind CUDA.
    # PaddleOCR 3.x removed `use_gpu` in favor of `device` and prefers `predict`.
    paddleocr_major = _parse_major_version(getattr(paddleocr_pkg, "__version__", "0"))
    if paddleocr_major >= 3:
        _ocr = PaddleOCR(
            lang="en",
            device="gpu:0",
            use_textline_orientation=True,
        )

        def _run_ocr(image_array: Any) -> Any:
            return list(_ocr.predict(image_array, use_textline_orientation=True))

    else:
        _ocr = PaddleOCR(
            use_angle_cls=True,
            lang="en",
            use_gpu=True,
        )

        def _run_ocr(image_array: Any) -> Any:
            return _ocr.ocr(image_array, cls=True)

    def pdf_to_images_high_quality(pdf_path: str, dpi: int = 144) -> list[Any]:
        """Rasterize PDF pages (PyMuPDF), same pattern as DeepSeek deploy."""
        import fitz
        from PIL import Image

        images: list[Any] = []
        pdf_document = fitz.open(pdf_path)
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        for page_num in range(pdf_document.page_count):
            page = pdf_document[page_num]
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            Image.MAX_IMAGE_PIXELS = None
            img_data = pixmap.tobytes("png")
            img = Image.open(io.BytesIO(img_data))
            if img.mode in ("RGBA", "LA"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                img = background
            images.append(img)
        pdf_document.close()
        return images

    async def _form_file_bytes(form: Any, field: str = "file") -> bytes:
        """Read multipart file without FastAPI File()/UploadFile injection (matches DeepSeek deploy)."""
        fil = form.get(field)
        if fil is None:
            raise HTTPException(status_code=400, detail=f"Missing multipart field '{field}'.")
        if hasattr(fil, "read"):
            data = await fil.read()
            return data if isinstance(data, (bytes, bytearray)) else bytes(data)
        if isinstance(fil, (bytes, bytearray)):
            return bytes(fil)
        return bytes(fil)

    def _form_bool(form: Any, key: str, default: bool) -> bool:
        v = form.get(key)
        if v is None:
            return default
        return str(v).lower() in ("true", "1", "yes", "on")

    def _form_int(form: Any, key: str, default: int) -> int:
        v = form.get(key)
        if v is None:
            return default
        return int(str(v))

    def _bundle_zip_bytes(markdown: str, metadata: dict[str, Any]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("markdown.md", markdown.encode("utf-8"))
            zf.writestr(
                "metadata.json",
                json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        return buf.getvalue()

    web = FastAPI(title="PaddleOCR (Modal)", version="1.0.0")

    @web.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @web.post("/ocr")
    async def ocr_endpoint(file: bytes = File(...)) -> JSONResponse:
        raw = file
        if not raw:
            raise HTTPException(status_code=400, detail="Empty body.")

        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"Invalid image: {e}") from e

        import numpy as np

        arr = np.array(img)
        result = _run_ocr(arr)
        return JSONResponse(content={"result": _json_safe(result)})

    @web.post("/ocr_pdf")
    async def ocr_pdf_endpoint(request: Request) -> Response:
        import numpy as np

        form = await request.form()
        raw = await _form_file_bytes(form, "file")
        if not raw:
            raise HTTPException(status_code=400, detail="Empty file.")

        max_pages = int(os.environ.get("MAX_PDF_PAGES", "200"))
        dpi = _form_int(form, "dpi", 144)
        include_raw_pages = _form_bool(form, "include_raw_pages", False)

        fd, pdf_path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            with open(pdf_path, "wb") as f:
                f.write(raw)
            images = pdf_to_images_high_quality(pdf_path, dpi=dpi)
        finally:
            try:
                os.unlink(pdf_path)
            except OSError:
                pass

        if len(images) > max_pages:
            raise HTTPException(
                status_code=400,
                detail=f"Too many pages: {len(images)} (max {max_pages}).",
            )

        page_split = "\n<--- Page Split --->\n"
        contents = ""
        pages_safe: list[Any] = []

        for img in images:
            arr = np.array(img.convert("RGB"))
            page_result = _run_ocr(arr)
            safe = _json_safe(page_result)
            pages_safe.append(safe)
            lines = _lines_from_paddle_result(page_result)
            contents += "\n".join(lines) + page_split

        meta: dict[str, Any] = {
            "page_count": len(images),
            "dpi": dpi,
            "paddleocr_version": getattr(paddleocr_pkg, "__version__", None),
            "pages": pages_safe,
        }
        if include_raw_pages:
            meta["raw_pages"] = pages_safe

        bundle_bytes = _bundle_zip_bytes(contents, meta)

        return Response(
            content=bundle_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="ocr-pdf-bundle.zip"'},
        )

    return web


@app.local_entrypoint()
async def main() -> None:
    """Print the HTTPS URL for this web app (use after `modal deploy` or `modal run`)."""
    url = await serve.get_web_url.aio()
    print(url)
