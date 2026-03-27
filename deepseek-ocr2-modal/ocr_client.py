#!/usr/bin/env python3
"""
POST a file to the deployed DeepSeek-OCR-2 API:

- Images (e.g. png, jpeg) → ``POST /ocr``
- PDF → ``POST /ocr_pdf``

The server returns ``application/zip`` with ``markdown.md``, ``metadata.json``, and optional
``images/*`` (cropped figures referenced as ``![](images/…)`` in the markdown).

This client prints ``markdown.md`` to stdout. If the zip contains files under ``images/``, it
also extracts the full bundle into a directory (default: ``<input_stem>.ocr`` next to the input)
so those relative links resolve. With no cropped images, nothing is written to disk.

Environment:
  DEEPSEEK_OCR_API_URL — base URL (no trailing slash), e.g.
    https://your-workspace--deepseek-ocr2-vllm-serve.modal.run

The ``modal`` package (modal-labs/modal-client) is for deploy and gRPC execution. Webhook /
``@modal.asgi_app()`` functions cannot be invoked with ``.remote()``; Modal expects you to call
the public URL over HTTP. Long runs use **303 See Other** to ``…?__modal_function_call_id=…``;
the follow-up request must be **GET** (RFC 7231). Re-POSTing to that URL yields
``400`` with ``modal-http: bad redirect method``. This client follows redirects explicitly and,
on read/connect timeouts (e.g. proxies), sleeps and retries the same URL. See also:
https://modal.com/docs/guide/webhook-timeouts
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

    Modal may hold each hop open ~150s; ``client`` should use a finite **read** timeout so a dead
    proxy can surface as ``ReadTimeout``—we then ``sleep`` and retry the same URL until
    ``deadline_monotonic``.
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
    """True if the archive contains at least one file under ``images/`` (model crops)."""
    for name in zf.namelist():
        if name.startswith("images/") and not name.endswith("/"):
            return True
    return False


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
        help="Print metadata.json from the bundle (pretty) instead of markdown.",
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
        help="Ask /ocr_pdf to include per-page raw model output (large metadata.json).",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help=(
            "Custom prompt sent to the API as form field 'prompt' (e.g. "
            "'<image>\\n<|grounding|>Convert the document to markdown.')."
        ),
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
        help=(
            "Seconds to sleep before retrying the same URL after a read/connect/write timeout "
            "(Modal long polls ~150s per hop; proxies may cut reads earlier). Default: 2."
        ),
    )
    parser.add_argument(
        "--modal-per-read-timeout",
        type=float,
        default=None,
        metavar="SEC",
        help=(
            "If set, cap each blocking read to this many seconds; on timeout the client sleeps "
            "(--modal-poll-sleep) and retries the same URL until the overall deadline. "
            "Use ~175 with a large job timeout if an intermediary drops idle connections before "
            "Modal's ~150s hop. Default: use the same value as the job timeout (single long read)."
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

    if not _is_zip_response(resp):
        try:
            payload = resp.json()
        except json.JSONDecodeError:
            print(
                "Expected application/zip from this API version.",
                file=sys.stderr,
            )
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


if __name__ == "__main__":
    main()
