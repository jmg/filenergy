"""OCR fallback for files that the local extractors couldn't read.

Strategy: send the file to Claude as a `document` content block (Claude
natively reads PDFs and images) and ask it to transcribe everything. This
avoids a Tesseract dependency and works on scanned PDFs and screenshots
out of the box.

Returns None when Claude isn't configured or the call fails — callers
treat OCR as best-effort and the file just stays unindexed.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import os

from filenergy import settings
from filenergy.services import chat

log = logging.getLogger(__name__)


_MAX_BYTES = 20 * 1024 * 1024  # Claude Files API caps inputs around this size

OCR_PROMPT = (
    "Transcribe ALL text in this document verbatim. Preserve paragraph "
    "breaks and ordering. Do not summarize, do not add commentary, do not "
    "wrap the result in code fences. If a region is illegible, write "
    "[illegible]."
)

_SUPPORTED_PDF = ("application/pdf",)
_SUPPORTED_IMAGE = ("image/png", "image/jpeg", "image/gif", "image/webp")


def is_configured() -> bool:
    return chat.is_configured()


def _media_type(path: str) -> str | None:
    mime, _ = mimetypes.guess_type(path)
    if mime in _SUPPORTED_PDF or mime in _SUPPORTED_IMAGE:
        return mime
    return None


def ocr_file(path: str) -> str | None:
    """Best-effort OCR. Returns extracted text or None on any failure."""
    if not is_configured():
        return None
    media_type = _media_type(path)
    if media_type is None:
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size <= 0 or size > _MAX_BYTES:
        return None

    try:
        with open(path, "rb") as fd:
            payload = base64.standard_b64encode(fd.read()).decode("ascii")
    except OSError:
        return None

    if media_type in _SUPPORTED_PDF:
        block = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": payload,
            },
        }
    else:
        block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": payload,
            },
        }

    try:
        client = chat._client()
        with client.messages.stream(
            model=settings.CLAUDE_MODEL,
            max_tokens=8000,
            messages=[{
                "role": "user",
                "content": [block, {"type": "text", "text": OCR_PROMPT}],
            }],
        ) as stream:
            response = stream.get_final_message()
        text = next(
            (b.text for b in response.content if getattr(b, "type", None) == "text"),
            "",
        ).strip()
        return text or None
    except Exception:
        log.exception("OCR via Claude failed for %s", path)
        return None
