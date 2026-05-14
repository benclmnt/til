#!/usr/bin/env python3
"""
POST a file to the deployed Modal PaddleOCR API:

- Images (e.g. png, jpeg) → ``POST /ocr`` (JSON: ``{"result": …}``)
- PDF → ``POST /ocr_pdf`` (``application/zip`` with ``markdown.md``, ``metadata.json``)

For PDFs the zip layout matches DeepSeek-OCR’s client expectations so the same unpack/print flow works.
``markdown.md`` contains plain text per page separated by ``<--- Page Split --->``; ``metadata.json``
includes ``pages`` (per-page OCR JSON).

The ``modal`` package is for deploy/gRPC. Webhook functions are called over HTTP. Long runs use
**303 See Other**; the follow-up request must be **GET**. This client follows redirects explicitly
and retries on read/connect timeouts. See: https://modal.com/docs/guide/webhook-timeouts

Environment:
  PADDLEOCR_API_URL — base URL (no trailing slash), e.g.
    https://your-workspace--paddleocr-modal-gpu-serve.modal.run
"""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


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
    """
    POST multipart once, then follow Modal’s **303** chain with **GET** (never re-POST the
    ``__modal_function_call_id`` URL).
    """
    current = url
    post_pending = True
    redirects = 0

    while redirects <= max_redirects:
        while True:
            if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
                raise TimeoutError(
                    "Modal web poll exceeded total deadline waiting for OCR response."
                )
            try:
                if post_pending:
                    resp = client.post(
                        current,
                        files=files,
                        data=data,
                        follow_redirects=False,
                    )
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


def _guess_content_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if mime:
        return mime
    suf = path.suffix.lower()
    if suf == ".pdf":
        return "application/pdf"
    return "application/octet-stream"


def _is_zip_response(resp: httpx.Response) -> bool:
    ct = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return ct == "application/zip"


def _zip_has_cropped_images(zf: zipfile.ZipFile) -> bool:
    """True if the archive contains at least one file under ``images/``."""
    for name in zf.namelist():
        if name.startswith("images/") and not name.endswith("/"):
            return True
    return False


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="POST an image or PDF to Modal PaddleOCR and print text or markdown.",
    )
    parser.add_argument("path", type=Path, help="Path to an image or .pdf file.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PADDLEOCR_API_URL", "").rstrip("/"),
        help="Deploy base URL (default: $PADDLEOCR_API_URL).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON (image) or metadata.json from the zip (PDF), pretty-printed.",
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
        help="Ask /ocr_pdf to duplicate per-page results under raw_pages in metadata.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "When the bundle includes images/, extract the zip here (default: "
            "<input_stem>.ocr beside the input file)."
        ),
    )
    parser.add_argument(
        "--modal-poll-sleep",
        type=float,
        default=2.0,
        metavar="SEC",
        help="Seconds to sleep before retrying after read/connect/write timeout. Default: 2.",
    )
    parser.add_argument(
        "--modal-per-read-timeout",
        type=float,
        default=None,
        metavar="SEC",
        help="Cap each blocking read to this many seconds; on timeout retry until deadline.",
    )
    args = parser.parse_args()

    if not args.base_url:
        print(
            "Missing API URL: set PADDLEOCR_API_URL or pass --base-url",
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

    data: dict[str, str] = {}
    if is_pdf:
        data["dpi"] = str(args.dpi)
        if args.raw_pages:
            data["include_raw_pages"] = "true"

    timeout = 1800.0 if is_pdf else 600.0
    files = {"file": (path.name, data_bytes, ctype)}
    read_cap = args.modal_per_read_timeout
    if read_cap is not None:
        read_cap = min(float(read_cap), timeout)
    read_timeout = read_cap if read_cap is not None else timeout
    httpx_timeout = httpx.Timeout(
        connect=60.0,
        read=read_timeout,
        write=timeout,
        pool=30.0,
    )
    deadline = time.monotonic() + timeout + 120.0
    try:
        with httpx.Client(timeout=httpx_timeout) as client:
            resp = _post_modal_web(
                client,
                url,
                files=files,
                data=data,
                poll_sleep_s=args.modal_poll_sleep,
                deadline_monotonic=deadline,
            )
    except TimeoutError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    except httpx.TooManyRedirects as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        print(e, file=sys.stderr)
        print(resp.text, file=sys.stderr)
        sys.exit(1)

    if is_pdf:
        if not _is_zip_response(resp):
            try:
                payload = resp.json()
            except json.JSONDecodeError:
                print("Expected application/zip from /ocr_pdf.", file=sys.stderr)
                print(resp.text[:2000], file=sys.stderr)
                sys.exit(1)
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                print(payload.get("markdown", ""))
            return

        buf = io.BytesIO(resp.content)
        with zipfile.ZipFile(buf, "r") as zf:
            try:
                md = zf.read("markdown.md").decode("utf-8")
            except KeyError:
                print("Bundle missing markdown.md", file=sys.stderr)
                sys.exit(1)
            has_images = _zip_has_cropped_images(zf)
            meta_bytes = zf.read("metadata.json") if "metadata.json" in zf.namelist() else None

            if has_images:
                out_dir = (
                    args.output_dir.expanduser()
                    if args.output_dir is not None
                    else path.with_name(f"{path.stem}.ocr")
                )
                out_dir.mkdir(parents=True, exist_ok=True)
                zf.extractall(out_dir)
                print(f"OCR bundle: {out_dir.resolve()}", file=sys.stderr)

            if args.json:
                if meta_bytes is None:
                    print("Bundle missing metadata.json", file=sys.stderr)
                    sys.exit(1)
                meta = json.loads(meta_bytes.decode("utf-8"))
                print(json.dumps(meta, indent=2, ensure_ascii=False))
                return

        print(md, end="" if md.endswith("\n") else "\n")
        return

    # POST /ocr — JSON
    payload = resp.json()
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    result = payload.get("result")
    for line in _lines_from_paddle_result(result):
        print(line)


if __name__ == "__main__":
    main()
