"""
Deploy [DeepSeek-OCR-2](https://github.com/deepseek-ai/DeepSeek-OCR-2) on Modal using **vLLM**
(`AsyncLLMEngine` + custom `DeepseekOCR2ForCausalLM`), matching the upstream
`DeepSeek-OCR2-vllm/run_dpsk_ocr2_image.py` flow — not `vllm serve` / OpenAI.

Upstream’s `requirements.txt` pins older `transformers`; vLLM 0.8.5 requires `transformers>=4.51.1`.
We install vLLM first and do not pin `transformers`/`tokenizers` so the resolver stays consistent with vLLM.

Upstream stack reference: CUDA 11.8 + torch 2.6 + vLLM 0.8.5. This image uses CUDA 12.4 + vLLM
0.8.5 (PyPI wheels built for recent CUDA), which matches v0.8.5’s supported install path on Linux.

``POST /ocr_pdf`` runs pages sequentially through the same ``AsyncLLMEngine`` as ``POST /ocr``. We do
not import ``run_dpsk_ocr2_pdf`` because that module constructs ``vllm.LLM`` at import time (a second
full model). PDF rasterization and layout drawing are inlined from that script.

Upstream ``sam_vary_sdpa.py`` imports ``flash_attn`` even though attention uses PyTorch SDPA; the
flash-attn call is commented out. The image build removes that import so we do not need a
``flash-attn`` compile (brittle on Modal).

Usage:
  cd deepseek-ocr2-modal
  uv sync
  uv run modal deploy deploy_deepseek_ocr2_modal.py

  uv run modal run deploy_deepseek_ocr2_modal.py

  DEEPSEEK_OCR_API_URL=https://…--deepseek-ocr2-vllm-serve.modal.run \\
    uv run python ocr_client.py page.png

  uv run python ocr_client.py doc.pdf

Environment (Modal function / dashboard):
  HF_MODEL_ID — default `deepseek-ai/DeepSeek-OCR-2`
  GPU_MEMORY_UTILIZATION — default `0.75`
  DEEPSEEK_OCR_PROMPT — override prompt (must contain a space for the image token when using OCR+VLM)
  DEEPSEEK_OCR_SKIP_REPEAT — default `false`. When `true`, `/ocr_pdf` skips only pages whose post-
    processed content is empty (repeat/no-EOS guard). Pages with non-empty OCR content are kept even
    if the model omits the ``|end▁of▁sentence|`` marker.
  MAX_PDF_PAGES — default `200` (reject larger PDFs at `/ocr_pdf`)
  HF_TOKEN — optional; set as a Modal Secret so Hub downloads are authenticated (fewer rate limits).
  HF_MODEL_REVISION (alias ``HF_REVISION``) — optional; git tag, branch name, or **commit SHA** on the
    Hub repo so weights + tokenizer + remote code stay fixed. Example: ``HF_MODEL_REVISION=abc123f``.
  HF_CODE_REVISION — optional; defaults to ``HF_MODEL_REVISION``. Pins the ``trust_remote_code``
    Python files separately if you ever need weights and code from different commits.
  HF_TOKENIZER_REVISION — optional; defaults to ``HF_MODEL_REVISION``.

  Find a commit under **Files and versions** → **History** on
  https://huggingface.co/deepseek-ai/DeepSeek-OCR-2 — copy the revision hash and set it as
  ``HF_MODEL_REVISION`` in the Modal app’s environment or a Secret.

Host env for deploy:
  MODAL_TOKEN_ID / MODAL_TOKEN_SECRET — https://modal.com/settings
"""

import io
import os
import tempfile
import threading
import time
import uuid
from typing import Any, Optional

import modal

# Same marker string as upstream `run_dpsk_ocr2_pdf.py` (repeat / no-EOS handling).
_REPEAT_END_SENTENCE = (
    b"<\xef\xbd\x9cend\xe2\x96\x81of\xe2\x96\x81sentence\xef\xbd\x9c>"
).decode("utf-8")
_REPEAT_END_SENTENCE_VARIANTS = (
    _REPEAT_END_SENTENCE,
    "<|end▁of▁sentence|>",
    "<|end_of_sentence|>",
    "<｜end_of_sentence｜>",
)


def _strip_end_sentence_markers(text: str) -> tuple[str, bool]:
    """Remove known end-of-sentence sentinels and report whether one was present."""
    had_marker = False
    for marker in _REPEAT_END_SENTENCE_VARIANTS:
        if marker in text:
            text = text.replace(marker, "")
            had_marker = True
    return text, had_marker

DS_ROOT = "/opt/DeepSeek-OCR-2/DeepSeek-OCR2-master/DeepSeek-OCR2-vllm"

deepseek_ocr_image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04",
        add_python="3.12",
    )
    .entrypoint([])
    .apt_install(
        "build-essential",
        "git",
        "libgomp1",
        "libglib2.0-0",
        "libgl1",
        "libsm6",
        "libxext6",
        "libxrender1",
    )
    .run_commands(
        "git clone --depth 1 --branch main https://github.com/deepseek-ai/DeepSeek-OCR-2.git /opt/DeepSeek-OCR-2",
        # Unused import; real path uses torch.nn.functional.scaled_dot_product_attention.
        "sed -i '/^from flash_attn import/d' /opt/DeepSeek-OCR-2/DeepSeek-OCR2-master/DeepSeek-OCR2-vllm/deepencoderv2/sam_vary_sdpa.py",
    )
    .pip_install("vllm==0.8.5")
    .pip_install(
        # Keep HF stack aligned with vLLM 0.8.5 and DeepSeek OCR tokenizer assumptions.
        "transformers==4.51.3",
        "tokenizers==0.21.1",
        "PyMuPDF",
        "img2pdf",
        "einops",
        "easydict",
        "addict",
        "Pillow>=10",
        "numpy>=1.26",
        "tqdm",
        "matplotlib",
        "fastapi>=0.115",
        "python-multipart>=0.0.9",
        "uvicorn[standard]>=0.30",
    )
    .env(
        {
            "VLLM_USE_V1": "0",
            "HF_HOME": "/root/.cache/huggingface",
            "CC": "gcc",
            "CXX": "g++",
        }
    )
)

hf_cache_vol = modal.Volume.from_name("deepseek-ocr2-hf-cache", create_if_missing=True)

app = modal.App("deepseek-ocr2-vllm")

MINUTES = 60
OCR_GPU = "A100"

_engine: Any = None
_engine_lock = threading.Lock()


@app.function(
    image=deepseek_ocr_image,
    gpu=OCR_GPU,
    timeout=30 * MINUTES,
    scaledown_window=10 * MINUTES,
    volumes={"/root/.cache/huggingface": hf_cache_vol},
)
@modal.concurrent(max_inputs=1)
@modal.asgi_app()
def serve():
    os.environ.setdefault("VLLM_USE_V1", "0")
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")

    import importlib.machinery
    import sys
    import types

    # `sam_vary_sdpa.py` imports flash_attn, but attention uses PyTorch SDPA; the flash call is
    # commented out. Image builds also strip the import via sed; this stub covers cached/old images.
    if "flash_attn" not in sys.modules:
        _flash = types.ModuleType("flash_attn")
        # `importlib.util.find_spec("flash_attn")` raises if a loaded module has no spec.
        _flash.__spec__ = importlib.machinery.ModuleSpec("flash_attn", loader=None)

        def flash_attn_qkvpacked_func(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("flash_attn_qkvpacked_func is not used in this deployment")

        _flash.flash_attn_qkvpacked_func = flash_attn_qkvpacked_func
        sys.modules["flash_attn"] = _flash

    sys.path.insert(0, DS_ROOT)
    os.chdir(DS_ROOT)

    import config as ds_config

    hf_revision = os.environ.get("HF_MODEL_REVISION") or os.environ.get("HF_REVISION")
    hf_code_revision = os.environ.get("HF_CODE_REVISION") or hf_revision
    hf_tokenizer_revision = os.environ.get("HF_TOKENIZER_REVISION") or hf_revision
    hf_token = os.environ.get("HF_TOKEN")

    if os.environ.get("HF_MODEL_ID"):
        ds_config.MODEL_PATH = os.environ["HF_MODEL_ID"]

    if os.environ.get("HF_MODEL_ID") or hf_revision or hf_token:
        from transformers import AutoTokenizer

        tok_kw: dict[str, Any] = {"trust_remote_code": True}
        if hf_tokenizer_revision:
            tok_kw["revision"] = hf_tokenizer_revision
        elif hf_revision:
            tok_kw["revision"] = hf_revision
        if hf_token:
            tok_kw["token"] = hf_token
        ds_config.TOKENIZER = AutoTokenizer.from_pretrained(ds_config.MODEL_PATH, **tok_kw)

    if os.environ.get("DEEPSEEK_OCR_PROMPT"):
        ds_config.PROMPT = os.environ["DEEPSEEK_OCR_PROMPT"]

    _skip_rep = os.environ.get("DEEPSEEK_OCR_SKIP_REPEAT", "false").strip().lower()
    ds_config.SKIP_REPEAT = _skip_rep in ("1", "true", "yes")

    import fitz
    import numpy as np

    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    from process.image_process import DeepseekOCR2Processor
    from process.ngram_norepeat import NoRepeatNGramLogitsProcessor
    from vllm import AsyncLLMEngine, SamplingParams
    from vllm.engine.arg_utils import AsyncEngineArgs

    import run_dpsk_ocr2_image as run_img_mod

    from run_dpsk_ocr2_image import (
        extract_coordinates_and_label,
        process_image_with_refs,
        re_match,
    )

    def load_image_bytes(data: bytes) -> Any:
        try:
            image = Image.open(io.BytesIO(data))
            image = ImageOps.exif_transpose(image)
            return image.convert("RGB")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid image: {e}") from e

    def pdf_to_images_high_quality(
        pdf_path: str, dpi: int = 144, image_format: str = "PNG"
    ) -> list[Any]:
        """Rasterize PDF pages (from upstream `run_dpsk_ocr2_pdf.py`)."""
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

    def draw_bounding_boxes_pdf(image: Any, refs: list[Any], jdx: int) -> Any:
        """PDF layout export path (per-page index `jdx`); from upstream `run_dpsk_ocr2_pdf.py`."""
        out_base = ds_config.OUTPUT_PATH
        image_width, image_height = image.size
        img_draw = image.copy()
        draw = ImageDraw.Draw(img_draw)
        overlay = Image.new("RGBA", img_draw.size, (0, 0, 0, 0))
        draw2 = ImageDraw.Draw(overlay)
        font = ImageFont.load_default()
        img_idx = 0
        for i, ref in enumerate(refs):
            try:
                result = extract_coordinates_and_label(ref, image_width, image_height)
                if result:
                    label_type, points_list = result
                    color = (
                        np.random.randint(0, 200),
                        np.random.randint(0, 200),
                        np.random.randint(0, 255),
                    )
                    color_a = color + (20,)
                    for points in points_list:
                        x1, y1, x2, y2 = points
                        x1 = int(x1 / 999 * image_width)
                        y1 = int(y1 / 999 * image_height)
                        x2 = int(x2 / 999 * image_width)
                        y2 = int(y2 / 999 * image_height)
                        if label_type == "image":
                            try:
                                cropped = image.crop((x1, y1, x2, y2))
                                cropped.save(f"{out_base}/images/{jdx}_{img_idx}.jpg")
                            except Exception:
                                pass
                            img_idx += 1
                        try:
                            if label_type == "title":
                                draw.rectangle([x1, y1, x2, y2], outline=color, width=4)
                                draw2.rectangle(
                                    [x1, y1, x2, y2],
                                    fill=color_a,
                                    outline=(0, 0, 0, 0),
                                    width=1,
                                )
                            else:
                                draw.rectangle([x1, y1, x2, y2], outline=color, width=2)
                                draw2.rectangle(
                                    [x1, y1, x2, y2],
                                    fill=color_a,
                                    outline=(0, 0, 0, 0),
                                    width=1,
                                )
                            text_x = x1
                            text_y = max(0, y1 - 15)
                            text_bbox = draw.textbbox((0, 0), label_type, font=font)
                            text_width = text_bbox[2] - text_bbox[0]
                            text_height = text_bbox[3] - text_bbox[1]
                            draw.rectangle(
                                [text_x, text_y, text_x + text_width, text_y + text_height],
                                fill=(255, 255, 255, 30),
                            )
                            draw.text((text_x, text_y), label_type, font=font, fill=color)
                        except Exception:
                            pass
            except Exception:
                continue
        img_draw.paste(overlay, (0, 0), overlay)
        return img_draw

    def process_pdf_image_with_refs(image: Any, ref_texts: list[Any], jdx: int) -> Any:
        return draw_bounding_boxes_pdf(image, ref_texts, jdx)

    def get_engine() -> Any:
        global _engine
        with _engine_lock:
            if _engine is None:
                util = float(os.environ.get("GPU_MEMORY_UTILIZATION", "0.75"))
                ea_kw: dict[str, Any] = {
                    "model": ds_config.MODEL_PATH,
                    "hf_overrides": {"architectures": ["DeepseekOCR2ForCausalLM"]},
                    "dtype": "bfloat16",
                    "max_model_len": 8192,
                    "enforce_eager": False,
                    "trust_remote_code": True,
                    "tensor_parallel_size": 1,
                    "gpu_memory_utilization": util,
                }
                if hf_revision:
                    ea_kw["revision"] = hf_revision
                if hf_code_revision:
                    ea_kw["code_revision"] = hf_code_revision
                if hf_tokenizer_revision:
                    ea_kw["tokenizer_revision"] = hf_tokenizer_revision
                if hf_token:
                    ea_kw["hf_token"] = hf_token
                engine_args = AsyncEngineArgs(**ea_kw)
                _engine = AsyncLLMEngine.from_engine_args(engine_args)
        return _engine

    async def stream_generate(image: Any, prompt: str, request_tag: str = "") -> str:
        engine = get_engine()
        logits_processors = [
            NoRepeatNGramLogitsProcessor(
                ngram_size=20,
                window_size=90,
                whitelist_token_ids={128821, 128822},
            )
        ]
        sampling_params = SamplingParams(
            temperature=0.0,
            max_tokens=8192,
            logits_processors=logits_processors,
            skip_special_tokens=False,
        )
        request_id = f"req-{time.time()}-{request_tag}-{uuid.uuid4().hex}"
        if image and " " in prompt:
            request: dict[str, Any] = {
                "prompt": prompt,
                "multi_modal_data": {"image": image},
            }
        elif prompt:
            request = {"prompt": prompt}
        else:
            raise HTTPException(status_code=400, detail="prompt is empty")

        final_output = ""
        async for request_output in engine.generate(request, sampling_params, request_id):
            if request_output.outputs:
                final_output = request_output.outputs[0].text
        return final_output

    web = FastAPI(title="DeepSeek-OCR-2 (vLLM)", version="1.0.0")

    async def _form_file_bytes(form: Any, field: str = "file") -> bytes:
        """Read multipart file without FastAPI File()/UploadFile injection (avoids Pydantic v2 issues on Modal)."""
        fil = form.get(field)
        if fil is None:
            raise HTTPException(status_code=400, detail=f"Missing multipart field '{field}'.")
        if hasattr(fil, "read"):
            data = await fil.read()
            return data if isinstance(data, (bytes, bytearray)) else bytes(data)
        if isinstance(fil, (bytes, bytearray)):
            return bytes(fil)
        return bytes(fil)

    def _form_optional_str(form: Any, key: str) -> Optional[str]:
        v = form.get(key)
        if v is None:
            return None
        s = str(v).strip()
        return s or None

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

    @web.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @web.post("/ocr")
    async def ocr_endpoint(request: Request) -> JSONResponse:
        form = await request.form()
        raw = await _form_file_bytes(form, "file")
        if not raw:
            raise HTTPException(status_code=400, detail="Empty file.")

        prompt = _form_optional_str(form, "prompt")
        use_prompt = prompt if prompt is not None else ds_config.PROMPT
        crop_mode = _form_bool(form, "crop_mode", True)
        ds_config.CROP_MODE = crop_mode

        image_pil = load_image_bytes(raw)

        if " " in use_prompt:
            image_features = DeepseekOCR2Processor().tokenize_with_images(
                images=[image_pil], bos=True, eos=True, cropping=crop_mode
            )
        else:
            image_features = ""

        result_out = await stream_generate(image_features, use_prompt)

        with tempfile.TemporaryDirectory() as tmp:
            ds_config.OUTPUT_PATH = tmp
            # run_dpsk_ocr2_image imported OUTPUT_PATH from config at load time; refresh the binding.
            run_img_mod.OUTPUT_PATH = tmp
            os.makedirs(f"{tmp}/images", exist_ok=True)

            outputs = result_out
            with open(f"{tmp}/result_ori.mmd", "w", encoding="utf-8") as afile:
                afile.write(outputs)

            matches_ref, matches_images, mathes_other = re_match(outputs)
            image_draw = image_pil.copy()
            result = process_image_with_refs(image_draw, matches_ref)

            for idx, a_match_image in enumerate(matches_images):
                outputs = outputs.replace(a_match_image, f"![](images/{idx}.jpg)\n")

            for _idx, a_match_other in enumerate(mathes_other):
                outputs = (
                    outputs.replace(a_match_other, "")
                    .replace("\\coloneqq", ":=")
                    .replace("\\eqqcolon", "=:")
                )

            with open(f"{tmp}/result.mmd", "w", encoding="utf-8") as afile:
                afile.write(outputs)

            if "line_type" in outputs:
                try:
                    import matplotlib.pyplot as plt
                    from matplotlib.patches import Circle

                    parsed_lines = eval(outputs)
                    lines = parsed_lines["Line"]["line"]
                    line_type = parsed_lines["Line"]["line_type"]
                    endpoints = parsed_lines["Line"]["line_endpoint"]

                    fig, ax = plt.subplots(figsize=(3, 3), dpi=200)
                    ax.set_xlim(-15, 15)
                    ax.set_ylim(-15, 15)

                    for idx, line in enumerate(lines):
                        try:
                            p0 = eval(line.split(" -- ")[0])
                            p1 = eval(line.split(" -- ")[-1])
                            if line_type[idx] == "--":
                                ax.plot([p0[0], p1[0]], [p0[1], p1[1]], linewidth=0.8, color="k")
                            else:
                                ax.plot([p0[0], p1[0]], [p0[1], p1[1]], linewidth=0.8, color="k")
                            ax.scatter(p0[0], p0[1], s=5, color="k")
                            ax.scatter(p1[0], p1[1], s=5, color="k")
                        except Exception:
                            pass

                    for endpoint in endpoints:
                        label = endpoint.split(": ")[0]
                        (x, y) = eval(endpoint.split(": ")[1])
                        ax.annotate(
                            label,
                            (x, y),
                            xytext=(1, 1),
                            textcoords="offset points",
                            fontsize=5,
                            fontweight="light",
                        )

                    if "Circle" in parsed_lines:
                        circle_centers = parsed_lines["Circle"]["circle_center"]
                        radius = parsed_lines["Circle"]["radius"]
                        for center, r in zip(circle_centers, radius):
                            center = eval(center.split(": ")[1])
                            circle = Circle(center, radius=r, fill=False, edgecolor="black", linewidth=0.8)
                            ax.add_patch(circle)

                    plt.savefig(f"{tmp}/geo.jpg")
                    plt.close()
                except Exception:
                    pass

            result.save(f"{tmp}/result_with_boxes.jpg")

        return JSONResponse(
            content={
                "markdown": outputs,
                "raw": result_out,
                "model_path": ds_config.MODEL_PATH,
                "hf_revision": hf_revision,
                "hf_code_revision": hf_code_revision,
            }
        )

    @web.post("/ocr_pdf")
    async def ocr_pdf_endpoint(request: Request) -> JSONResponse:
        form = await request.form()
        raw = await _form_file_bytes(form, "file")
        if not raw:
            raise HTTPException(status_code=400, detail="Empty file.")

        max_pages = int(os.environ.get("MAX_PDF_PAGES", "200"))
        prompt = _form_optional_str(form, "prompt")
        use_prompt = prompt if prompt is not None else ds_config.PROMPT
        crop_mode = _form_bool(form, "crop_mode", True)
        dpi = _form_int(form, "dpi", 144)
        include_raw_pages = _form_bool(form, "include_raw_pages", False)
        ds_config.CROP_MODE = crop_mode

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

        contents_det = ""
        contents = ""
        raw_pages: list[str] = []
        pages_missing_eos = 0
        pages_skipped = 0

        with tempfile.TemporaryDirectory() as tmp:
            ds_config.OUTPUT_PATH = tmp
            run_img_mod.OUTPUT_PATH = tmp
            os.makedirs(f"{tmp}/images", exist_ok=True)

            for jdx, img in enumerate(images):
                if " " in use_prompt:
                    image_features = DeepseekOCR2Processor().tokenize_with_images(
                        images=[img], bos=True, eos=True, cropping=crop_mode
                    )
                else:
                    image_features = ""

                result_out = await stream_generate(image_features, use_prompt, request_tag=str(jdx))
                raw_pages.append(result_out)
                content = result_out

                content, has_eos = _strip_end_sentence_markers(content)
                if not has_eos:
                    pages_missing_eos += 1
                if ds_config.SKIP_REPEAT and not content.strip():
                    pages_skipped += 1
                    continue

                page_split = "\n<--- Page Split --->\n"
                contents_det += content + page_split

                image_draw = img.copy()
                matches_ref, matches_images, mathes_other = re_match(content)
                _ = process_pdf_image_with_refs(image_draw, matches_ref, jdx)

                for idx, a_match_image in enumerate(matches_images):
                    content = content.replace(
                        a_match_image, f"![](images/{jdx}_{idx}.jpg)\n"
                    )

                for _idx, a_match_other in enumerate(mathes_other):
                    content = (
                        content.replace(a_match_other, "")
                        .replace("\\coloneqq", ":=")
                        .replace("\\eqqcolon", "=:")
                        .replace("\n\n\n\n", "\n\n")
                        .replace("\n\n\n", "\n\n")
                    )

                contents += content + page_split

                if "line_type" in content:
                    try:
                        import matplotlib.pyplot as plt
                        from matplotlib.patches import Circle

                        parsed_lines = eval(content)
                        lines = parsed_lines["Line"]["line"]
                        line_type = parsed_lines["Line"]["line_type"]
                        endpoints = parsed_lines["Line"]["line_endpoint"]

                        fig, ax = plt.subplots(figsize=(3, 3), dpi=200)
                        ax.set_xlim(-15, 15)
                        ax.set_ylim(-15, 15)

                        for idx, line in enumerate(lines):
                            try:
                                p0 = eval(line.split(" -- ")[0])
                                p1 = eval(line.split(" -- ")[-1])
                                if line_type[idx] == "--":
                                    ax.plot(
                                        [p0[0], p1[0]],
                                        [p0[1], p1[1]],
                                        linewidth=0.8,
                                        color="k",
                                    )
                                else:
                                    ax.plot(
                                        [p0[0], p1[0]],
                                        [p0[1], p1[1]],
                                        linewidth=0.8,
                                        color="k",
                                    )
                                ax.scatter(p0[0], p0[1], s=5, color="k")
                                ax.scatter(p1[0], p1[1], s=5, color="k")
                            except Exception:
                                pass

                        for endpoint in endpoints:
                            label = endpoint.split(": ")[0]
                            (x, y) = eval(endpoint.split(": ")[1])
                            ax.annotate(
                                label,
                                (x, y),
                                xytext=(1, 1),
                                textcoords="offset points",
                                fontsize=5,
                                fontweight="light",
                            )

                        if "Circle" in parsed_lines:
                            circle_centers = parsed_lines["Circle"]["circle_center"]
                            radius = parsed_lines["Circle"]["radius"]
                            for center, r in zip(circle_centers, radius):
                                center = eval(center.split(": ")[1])
                                circle = Circle(
                                    center,
                                    radius=r,
                                    fill=False,
                                    edgecolor="black",
                                    linewidth=0.8,
                                )
                                ax.add_patch(circle)

                        plt.savefig(f"{tmp}/geo_{jdx}.jpg")
                        plt.close()
                    except Exception:
                        pass

        payload: dict[str, Any] = {
            "markdown": contents,
            "markdown_det": contents_det,
            "page_count": len(images),
            "skip_repeat": bool(ds_config.SKIP_REPEAT),
            "pages_missing_eos": pages_missing_eos,
            "pages_skipped": pages_skipped,
            "model_path": ds_config.MODEL_PATH,
            "hf_revision": hf_revision,
            "hf_code_revision": hf_code_revision,
        }
        if include_raw_pages:
            payload["raw_pages"] = raw_pages
        return JSONResponse(content=payload)

    return web


@app.local_entrypoint()
async def main() -> None:
    url = await serve.get_web_url.aio()
    print(url)
