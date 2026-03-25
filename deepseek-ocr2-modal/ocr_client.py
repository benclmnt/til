#!/usr/bin/env python3
"""
POST a file to the deployed DeepSeek-OCR-2 API:

- Images (e.g. png, jpeg) → ``POST /ocr``
- PDF → ``POST /ocr_pdf``

Response JSON (image): ``markdown``, ``raw``, ``model_path``.

Response JSON (PDF): ``markdown``, ``markdown_det``, ``page_count``, ``model_path``;
optional ``raw_pages`` if ``--raw-pages``.

Environment:
  DEEPSEEK_OCR_API_URL — base URL (no trailing slash), e.g.
    https://your-workspace--deepseek-ocr2-vllm-serve.modal.run
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path

import httpx


def _guess_content_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if mime:
        return mime
    suf = path.suffix.lower()
    if suf == ".pdf":
        return "application/pdf"
    return "application/octet-stream"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="POST an image or PDF to Modal DeepSeek-OCR-2 (vLLM) and print markdown.",
    )
    parser.add_argument("path", type=Path, help="Path to an image or .pdf file.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("DEEPSEEK_OCR_API_URL", "").rstrip("/"),
        help="Deploy base URL (default: $DEEPSEEK_OCR_API_URL).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw JSON response.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=144,
        help="PDF rasterization DPI for /ocr_pdf (default: 144).",
    )
    parser.add_argument(
        "--raw-pages",
        action="store_true",
        help="Ask /ocr_pdf to include per-page raw model output (large JSON).",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help=(
            "Custom prompt sent to the API as form field 'prompt' (e.g. "
            "'<image>\\n<|grounding|>Convert the document to markdown.')."
        ),
    )
    args = parser.parse_args()

    if not args.base_url:
        print(
            "Missing API URL: set DEEPSEEK_OCR_API_URL or pass --base-url",
            file=sys.stderr,
        )
        sys.exit(2)

    path = args.path.expanduser()
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        sys.exit(2)

    is_pdf = path.suffix.lower() == ".pdf"
    route = "ocr_pdf" if is_pdf else "ocr"
    url = f"{args.base_url}/{route}"
    ctype = _guess_content_type(path)
    data_bytes = path.read_bytes()

    data: dict[str, str] = {"crop_mode": "true"}
    if args.prompt is not None:
        data["prompt"] = args.prompt

    timeout = 600.0
    if is_pdf:
        data["dpi"] = str(args.dpi)
        if args.raw_pages:
            data["include_raw_pages"] = "true"
        timeout = 1800.0

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            url,
            files={"file": (path.name, data_bytes, ctype)},
            data=data,
        )

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        print(e, file=sys.stderr)
        print(resp.text, file=sys.stderr)
        sys.exit(1)

    payload = resp.json()
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    md = payload.get("markdown", "")
    print(md)


if __name__ == "__main__":
    main()
