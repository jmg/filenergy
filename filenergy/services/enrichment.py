"""On-index enrichment: per-file summary + suggested questions via Claude.

Runs once after a file is successfully indexed. Output goes onto
`File.summary` and `File.suggested_questions_json`. All errors swallowed —
this is a "nice to have" pass; if it fails, the file stays indexed.
"""
from __future__ import annotations

import json
import logging

from filenergy import db, settings
from filenergy.services import chat

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """You analyze a single document and produce a JSON object \
with two fields: `summary` (one or two plain sentences, max 240 chars) and \
`questions` (an array of exactly 3 short interesting questions a reader \
might ask about THIS document, no overlap, no preamble). Respond with \
valid JSON only — no markdown fences, no commentary."""


_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["summary", "questions"],
    "additionalProperties": False,
}


def is_configured() -> bool:
    return chat.is_configured()


def enrich_file(file) -> bool:
    """Generate and persist `summary` + `suggested_questions_json` on `file`.

    Returns True on success. Failure swallowed — caller already committed
    the indexed state, so a missing summary is acceptable.
    """
    if not is_configured():
        return False
    text = file.text_content
    if not text:
        return False

    excerpt = text[:6000]  # bound the prompt; we don't need the whole doc
    user_message = (
        f"<filename>{file.name}</filename>\n"
        f"<content>\n{excerpt}\n</content>"
    )
    try:
        client = chat._client()
        # Use `.stream()` so timeouts can't strand a long-running call;
        # the SDK helper accumulates the response for us.
        with client.messages.stream(
            model=settings.CLAUDE_MODEL,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
            output_config={
                "format": {"type": "json_schema", "schema": _SCHEMA}
            },
        ) as stream:
            response = stream.get_final_message()
        text_out = next(
            (b.text for b in response.content if getattr(b, "type", None) == "text"),
            "",
        ).strip()
        if not text_out:
            return False
        parsed = json.loads(text_out)
        summary = (parsed.get("summary") or "").strip()[:500]
        questions = parsed.get("questions") or []
        if not isinstance(questions, list):
            questions = []
        questions = [str(q).strip()[:240] for q in questions if str(q).strip()][:3]
        file.summary = summary or None
        file.suggested_questions_json = json.dumps(questions) if questions else None
        db.session.commit()
        return True
    except Exception:
        log.exception("Enrichment failed for %s", file.name)
        db.session.rollback()
        return False
