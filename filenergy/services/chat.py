"""RAG chat: retrieve relevant chunks + answer with Claude.

Two surfaces:
- `answer_question` — single shot, returns final text + sources.
- `stream_answer` — yields SSE-shaped strings for real-time UI streaming.

Both use prompt caching on the system prompt and adaptive thinking.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Iterable

from filenergy import settings
from filenergy.models import File, Message
from filenergy.services import embeddings


SYSTEM_PROMPT = """You are Filenergy's archivist. The user uploads documents \
to their personal Filenergy library and asks questions about them.

Answer using ONLY the excerpts in <context>. If the answer isn't in the \
context, say so plainly — do not speculate or use outside knowledge.

When you cite a fact, mention the source filename in parentheses, e.g. \
"(report.pdf)". Be concise and concrete. Use Markdown formatting (lists, \
bold, code blocks) when it improves readability."""


class ChatUnavailable(RuntimeError):
    """Raised when the Anthropic API key or SDK isn't configured."""


@dataclass
class Source:
    file_id: int
    name: str
    url: str
    score: float


@dataclass
class Answer:
    text: str
    sources: list[Source]
    # Per-chunk provenance: (chunk_id, score) for every retrieved chunk that
    # made it into the prompt. Used by conversations.add_assistant_message
    # to populate the MessageCitation index.
    chunk_citations: list = None  # type: ignore[assignment]


@lru_cache(maxsize=1)
def _client():
    if not settings.ANTHROPIC_API_KEY:
        raise ChatUnavailable(
            "ANTHROPIC_API_KEY is not set. Configure it to enable /ask."
        )
    try:
        import anthropic
    except ImportError as exc:
        raise ChatUnavailable("anthropic package not installed") from exc
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def is_configured() -> bool:
    return bool(settings.ANTHROPIC_API_KEY) and embeddings.is_configured()


def _build_context(retrieved):
    """Returns (context_str, sources_sorted, chunk_citations)."""
    blocks = []
    sources: dict[int, Source] = {}
    chunk_citations: list[tuple[int, float]] = []
    for chunk, score in retrieved:
        f: File = chunk.file
        blocks.append(
            f"<excerpt source=\"{f.name}\" chunk=\"{chunk.position}\">\n"
            f"{chunk.content}\n"
            f"</excerpt>"
        )
        chunk_citations.append((chunk.id, float(score)))
        if f.id not in sources or sources[f.id].score < score:
            sources[f.id] = Source(
                file_id=f.id, name=f.name, url=f.url, score=score
            )
    context = "<context>\n" + "\n\n".join(blocks) + "\n</context>"
    return (
        context,
        sorted(sources.values(), key=lambda s: -s.score),
        chunk_citations,
    )


def _build_messages(
    conversation_messages, context: str, question: str,
    *, images: list[dict] | None = None,
) -> list[dict]:
    """Compose the prior turns + this turn into Anthropic messages.

    Prior turns are included verbatim; the new turn is prefixed with the
    retrieved context for RAG grounding.

    `images` is an optional list of `{"media_type": "image/png",
    "data": "<base64>"}` dicts that get prepended to the new turn's
    content as Claude `image` blocks. Vision turns the chat into a
    "look at this screenshot and reason against my docs" experience.
    """
    messages: list[dict] = []
    for m in conversation_messages or []:
        messages.append({"role": m.role, "content": m.content})

    text_block = {
        "type": "text",
        "text": f"{context}\n\n<question>{question}</question>",
    }
    if images:
        content: list[dict] = []
        for img in images:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["media_type"],
                    "data": img["data"],
                },
            })
        content.append(text_block)
        messages.append({"role": "user", "content": content})
    else:
        # Plain text — keep the legacy string shape so existing tests
        # / API clients see no behaviour change.
        messages.append({
            "role": "user",
            "content": text_block["text"],
        })
    return messages


def _retrieve(workspace, question: str, *,
              collection_id: int | None = None,
              file_id: int | None = None):
    return embeddings.search(
        workspace, question, settings.RETRIEVAL_K,
        collection_id=collection_id, file_id=file_id,
    )


def _no_results_message() -> str:
    return (
        "No matching content found in your library. "
        "Upload files first, or check that indexing succeeded."
    )


def answer_question(
    workspace, question: str, history: Iterable[Message] = (),
    *, collection_id: int | None = None, file_id: int | None = None,
    images: list[dict] | None = None,
) -> Answer:
    retrieved = _retrieve(
        workspace, question, collection_id=collection_id, file_id=file_id
    )
    if not retrieved and not images:
        return Answer(text=_no_results_message(), sources=[], chunk_citations=[])

    context, sources, chunk_citations = _build_context(retrieved)
    messages = _build_messages(list(history), context, question, images=images)

    with _client().messages.stream(
        model=settings.CLAUDE_MODEL,
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=messages,
    ) as stream:
        message = stream.get_final_message()

    text = next(
        (b.text for b in message.content if getattr(b, "type", None) == "text"),
        "",
    ).strip()
    return Answer(
        text=text or "(no answer)",
        sources=sources,
        chunk_citations=chunk_citations,
    )


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def stream_answer(
    workspace, question: str, history: Iterable[Message] = (),
    *, collection_id: int | None = None, file_id: int | None = None,
    images: list[dict] | None = None,
) -> Iterable[str]:
    """Yield SSE-formatted strings for an EventSource consumer.

    Events:
        - token: incremental text delta ({"text": "..."})
        - done:  final payload ({"text": "...", "sources": [...]})
        - error: ({"message": "..."})

    `images` is an optional list of base64'd image dicts; when provided
    Claude gets a vision-capable user turn.
    """
    try:
        retrieved = _retrieve(
            workspace, question, collection_id=collection_id, file_id=file_id
        )
    except Exception as exc:  # network, auth, etc.
        yield _sse("error", {"message": str(exc)})
        return

    if not retrieved and not images:
        text = _no_results_message()
        yield _sse("token", {"text": text})
        yield _sse("done", {"text": text, "sources": [], "chunk_citations": []})
        return

    context, sources, chunk_citations = _build_context(retrieved)
    messages = _build_messages(list(history), context, question, images=images)

    parts: list[str] = []
    try:
        with _client().messages.stream(
            model=settings.CLAUDE_MODEL,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
        ) as stream:
            for delta in stream.text_stream:
                parts.append(delta)
                yield _sse("token", {"text": delta})
    except Exception as exc:
        yield _sse("error", {"message": str(exc)})
        return

    text = ("".join(parts)).strip() or "(no answer)"
    yield _sse(
        "done",
        {
            "text": text,
            "sources": [asdict(s) for s in sources],
            "chunk_citations": chunk_citations,
        },
    )
