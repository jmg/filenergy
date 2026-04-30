"""Text extraction for indexable file types.

Returns plain UTF-8 text or None when the file type is not supported.
"""
from __future__ import annotations

import mimetypes
import os


def _read_text(path: str) -> str | None:
    for encoding in ("utf-8", "latin-1"):
        try:
            with open(path, encoding=encoding) as fd:
                return fd.read()
        except UnicodeDecodeError:
            continue
    return None


def _read_pdf(path: str) -> str | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None

    try:
        reader = PdfReader(path)
    except Exception:
        return None

    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n\n".join(p for p in parts if p.strip())
    return text or None


def _read_docx(path: str) -> str | None:
    try:
        import docx
    except ImportError:
        return None

    try:
        document = docx.Document(path)
    except Exception:
        return None

    return "\n".join(p.text for p in document.paragraphs if p.text)


_HANDLERS = {
    ".pdf": _read_pdf,
    ".docx": _read_docx,
    ".txt": _read_text,
    ".md": _read_text,
    ".markdown": _read_text,
    ".rst": _read_text,
    ".csv": _read_text,
    ".json": _read_text,
    ".html": _read_text,
    ".htm": _read_text,
    ".log": _read_text,
    ".py": _read_text,
    ".js": _read_text,
    ".ts": _read_text,
}


_OCR_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf"}


def is_indexable(filename: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    if ext in _HANDLERS:
        return True
    mime, _ = mimetypes.guess_type(filename)
    if mime and mime.startswith("text/"):
        return True
    # OCR-eligible types: regular extraction returns empty, then index_file
    # falls back to Claude vision.
    return ext in _OCR_EXTS


def extract_text(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    handler = _HANDLERS.get(ext)
    if handler is None:
        mime, _ = mimetypes.guess_type(path)
        if mime and mime.startswith("text/"):
            handler = _read_text
    if handler is None:
        return None
    return handler(path)


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Split text into overlapping chunks on paragraph/sentence boundaries when possible."""
    text = text.strip()
    if not text:
        return []

    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            # Prefer to break on a paragraph or sentence boundary near `end`.
            window = text[start:end]
            for sep in ("\n\n", "\n", ". ", " "):
                idx = window.rfind(sep)
                if idx > size // 2:
                    end = start + idx + len(sep)
                    break
        chunks.append(text[start:end].strip())
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]
