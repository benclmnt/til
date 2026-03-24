#!/usr/bin/env python3
"""
Send an image to the deployed Modal PaddleOCR `/ocr` endpoint and print recognized text.

API response shape (`POST /ocr`, multipart field `file`):
  {"result": <paddle_ocr_result>}

PaddleOCR JSON after `_json_safe` differs by major version:
  - 2.x: nested OCR tuples, e.g. `[[[box, [text, score]], ...]]`
  - 3.x: list of result dicts containing `res.rec_texts`

Environment:
  PADDLEOCR_API_URL — base URL of the deployed app (no trailing slash), e.g.
    https://your-workspace--paddleocr-modal-gpu-serve.modal.run
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any

import httpx


def _lines_from_paddle_result(result: Any) -> list[str]:
    """Turn PaddleOCR 2.x/3.x outputs into plain text lines."""
    if result is None:
        return []
    if isinstance(result, dict):
        result = [result]

    lines: list[str] = []
    for page in result:
        if page is None:
            continue

        # PaddleOCR 3.x JSON shape (from `res.json`) includes `rec_texts`.
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


def _guess_content_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if mime and mime.startswith("image/"):
        return mime
    return "image/png"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="POST an image to Modal PaddleOCR and print recognized text lines.",
    )
    parser.add_argument(
        "image",
        type=Path,
        help="Path to an image file (png, jpeg, webp, …).",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PADDLEOCR_API_URL", "").rstrip("/"),
        help="Deploy base URL (default: $PADDLEOCR_API_URL). Example: "
        "https://workspace--paddleocr-modal-gpu-serve.modal.run",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw JSON response to stdout instead of text lines.",
    )
    args = parser.parse_args()

    if not args.base_url:
        print(
            "Missing API URL: set PADDLEOCR_API_URL or pass --base-url",
            file=sys.stderr,
        )
        sys.exit(2)

    path = args.image.expanduser()
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        sys.exit(2)

    url = f"{args.base_url}/ocr"
    ctype = _guess_content_type(path)
    data = path.read_bytes()

    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            url,
            files={"file": (path.name, data, ctype)},
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

    result = payload.get("result")
    for line in _lines_from_paddle_result(result):
        print(line)


if __name__ == "__main__":
    main()
