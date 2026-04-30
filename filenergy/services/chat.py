"""RAG chat: retrieve relevant chunks + answer with Claude.

Uses prompt caching on the system prompt and streams the response so that
adaptive thinking + long answers don't trip request timeouts.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from filenergy import settings
from filenergy.models import File
from filenergy.services import embeddings


SYSTEM_PROMPT = """You are Filenergy's archivist. The user uploads documents \
to their personal Filenergy library and asks questions about them.

Answer using ONLY the excerpts in <context>. If the answer isn't in the \
context, say so plainly — do not speculate or use outside knowledge.

When you cite a fact, mention the source filename in parentheses, e.g. \
"(report.pdf)". Be concise and concrete."""


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


def _build_context(retrieved) -> tuple[str, list[Source]]:
    blocks = []
    sources: dict[int, Source] = {}
    for chunk, score in retrieved:
        f: File = chunk.file
        blocks.append(
            f"<excerpt source=\"{f.name}\" chunk=\"{chunk.position}\">\n"
            f"{chunk.content}\n"
            f"</excerpt>"
        )
        if f.id not in sources or sources[f.id].score < score:
            sources[f.id] = Source(
                file_id=f.id, name=f.name, url=f.url, score=score
            )
    context = "<context>\n" + "\n\n".join(blocks) + "\n</context>"
    return context, sorted(sources.values(), key=lambda s: -s.score)


def answer_question(user, question: str) -> Answer:
    retrieved = embeddings.search(user, question, settings.RETRIEVAL_K)
    if not retrieved:
        return Answer(
            text=(
                "No matching content found in your library. "
                "Upload files first, or check that indexing succeeded."
            ),
            sources=[],
        )

    context, sources = _build_context(retrieved)
    user_message = f"{context}\n\n<question>{question}</question>"

    client = _client()

    with client.messages.stream(
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
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        message = stream.get_final_message()

    text = next(
        (b.text for b in message.content if getattr(b, "type", None) == "text"),
        "",
    ).strip()
    return Answer(text=text or "(no answer)", sources=sources)
