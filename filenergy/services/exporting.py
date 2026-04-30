"""Export a Conversation to PDF or DOCX.

PDF uses fpdf2 (pure Python, no Cairo); DOCX uses python-docx (already
required for indexing). Both functions return raw bytes so the view
layer can wrap them in a Flask Response.
"""
from __future__ import annotations

import io
import json
import logging
from typing import Iterable

log = logging.getLogger(__name__)


class ExportUnavailable(RuntimeError):
    pass


def _iter_messages(conversation) -> Iterable:
    return list(conversation.messages)


def _conversation_title(conversation) -> str:
    return conversation.title or f"Conversation {conversation.id}"


def _sources(msg) -> list[str]:
    if not msg.sources_json:
        return []
    try:
        items = json.loads(msg.sources_json)
    except Exception:
        return []
    return [str(item.get("name", "?")) for item in items if isinstance(item, dict)]


def to_markdown(conversation) -> str:
    """Same formatting as the /export.md endpoint, exposed as a service."""
    lines: list[str] = [f"# {_conversation_title(conversation)}", ""]
    if conversation.created_at:
        lines.append(f"_Created {conversation.created_at.strftime('%Y-%m-%d %H:%M')}_")
        lines.append("")
    for msg in _iter_messages(conversation):
        speaker = "**You**" if msg.role == "user" else "**Assistant**"
        lines.append(speaker)
        lines.append("")
        lines.append(msg.content or "")
        lines.append("")
        sources = _sources(msg)
        if sources and msg.role == "assistant":
            lines.append("Sources: " + ", ".join(sources))
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def to_pdf(conversation) -> bytes:
    try:
        from fpdf import FPDF
    except ImportError as exc:
        raise ExportUnavailable("fpdf2 is not installed") from exc

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    # Explicit width so multi_cell knows where to wrap.
    text_w = pdf.w - pdf.l_margin - pdf.r_margin

    title = _conversation_title(conversation)
    pdf.set_font("Helvetica", "B", 18)
    pdf.multi_cell(text_w, 8, _ascii(title))
    pdf.ln(2)

    if conversation.created_at:
        pdf.set_font("Helvetica", "I", 10)
        pdf.set_text_color(120, 120, 120)
        pdf.multi_cell(
            text_w, 5,
            _ascii(f"Created {conversation.created_at.strftime('%Y-%m-%d %H:%M')}"),
        )
        pdf.set_text_color(0, 0, 0)
        pdf.ln(4)

    for msg in _iter_messages(conversation):
        pdf.set_font("Helvetica", "B", 11)
        speaker = "You" if msg.role == "user" else "Assistant"
        pdf.multi_cell(text_w, 5, speaker)

        pdf.set_font("Helvetica", "", 11)
        pdf.multi_cell(text_w, 5, _ascii(msg.content or " "))

        sources = _sources(msg)
        if sources and msg.role == "assistant":
            pdf.set_font("Helvetica", "I", 9)
            pdf.set_text_color(120, 120, 120)
            pdf.multi_cell(text_w, 4, _ascii("Sources: " + ", ".join(sources)))
            pdf.set_text_color(0, 0, 0)
        pdf.ln(3)

    out = pdf.output(dest="S")
    if isinstance(out, str):
        return out.encode("latin-1")
    if isinstance(out, bytearray):
        return bytes(out)
    return out


def to_docx(conversation) -> bytes:
    try:
        import docx
    except ImportError as exc:
        raise ExportUnavailable("python-docx is not installed") from exc

    document = docx.Document()
    document.add_heading(_conversation_title(conversation), level=1)
    if conversation.created_at:
        p = document.add_paragraph(
            f"Created {conversation.created_at.strftime('%Y-%m-%d %H:%M')}"
        )
        for run in p.runs:
            run.italic = True

    for msg in _iter_messages(conversation):
        speaker = "You" if msg.role == "user" else "Assistant"
        heading = document.add_paragraph()
        run = heading.add_run(speaker)
        run.bold = True

        document.add_paragraph(msg.content or "")

        sources = _sources(msg)
        if sources and msg.role == "assistant":
            sub = document.add_paragraph()
            sr = sub.add_run("Sources: " + ", ".join(sources))
            sr.italic = True

    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _ascii(s: str) -> str:
    """fpdf2's default Helvetica is latin-1 only. Strip anything else."""
    return s.encode("latin-1", errors="replace").decode("latin-1")
