"""LLM-based reranking on top of embedding retrieval.

Embeddings get you a *recall* layer — you pull back roughly-relevant
chunks fast. They're not great at *precision* — figuring out which 4
of 20 candidates actually answer the question. A small cross-encoder
or LLM re-scoring pass on the top-K cuts the chaff before grounding.

Two backends:

  - "claude" (default when ANTHROPIC_API_KEY is set) — sends every
    candidate + the query as a single prompt and asks Claude Haiku for
    a 0-10 score per chunk. Cheap because we use the small/fast model
    and structured output.
  - "noop" — return the input unchanged. Lets self-hosted operators
    disable the extra round-trip. Set FILENERGY_RERANKER=noop.

Tunables:
  - FILENERGY_RERANKER=claude|noop          (default: claude)
  - FILENERGY_RERANKER_MODEL=claude-haiku-4-5-20251001
  - FILENERGY_RERANK_TOP_K=4                (results to keep)
  - FILENERGY_RERANK_CANDIDATES=20          (max sent to the reranker)
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)


def _backend() -> str:
    return (os.environ.get("FILENERGY_RERANKER") or "claude").lower()


def _model() -> str:
    return os.environ.get("FILENERGY_RERANKER_MODEL", "claude-haiku-4-5-20251001")


def _top_k() -> int:
    try:
        return int(os.environ.get("FILENERGY_RERANK_TOP_K", "4"))
    except (TypeError, ValueError):
        return 4


def _candidates_cap() -> int:
    try:
        return int(os.environ.get("FILENERGY_RERANK_CANDIDATES", "20"))
    except (TypeError, ValueError):
        return 20


def is_enabled() -> bool:
    """Whether the active backend can actually rank.

    Claude backend needs `ANTHROPIC_API_KEY`; noop is always available
    but is a passthrough.
    """
    backend = _backend()
    if backend == "noop":
        return False
    if backend == "claude":
        from filenergy import settings
        return bool(getattr(settings, "ANTHROPIC_API_KEY", None))
    return False


def rerank(query: str, candidates: list) -> list:
    """Re-score `candidates` (list of `(Chunk, score)` tuples) and return
    the top-K by the reranker's verdict. Falls through unchanged if the
    backend is disabled or errors.

    The order of the output is the new ranking; entries below cut-off
    are dropped.
    """
    if not candidates:
        return candidates
    if not is_enabled():
        return candidates[:_top_k()]

    backend = _backend()
    if backend == "claude":
        try:
            return _rerank_claude(query, candidates)
        except Exception:
            log.exception("Claude reranker failed; falling back to embeddings order")
            return candidates[:_top_k()]
    return candidates[:_top_k()]


def _rerank_claude(query: str, candidates: list) -> list:
    """Use the small/fast Claude model to score every candidate 0-10
    against the query. We send a single message with all candidates so
    the model can read them in context (reranking is cross-attention).
    """
    from filenergy.services.chat import _client

    capped = candidates[: _candidates_cap()]
    numbered = []
    for i, (chunk, _emb_score) in enumerate(capped):
        text = (chunk.content or "")[:600]
        numbered.append(f"<chunk id=\"{i}\">{text}</chunk>")

    prompt = (
        "You score document chunks for relevance to a user query.\n"
        "Return STRICT JSON like {\"scores\":[{\"id\":0,\"score\":7},...]}\n"
        "score is an integer 0-10; 0 = unrelated, 10 = directly answers.\n\n"
        f"<query>{query}</query>\n\n"
        + "\n".join(numbered)
    )

    msg = _client().messages.create(
        model=_model(),
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(
        b.text for b in msg.content
        if getattr(b, "type", None) == "text"
    ).strip()

    # The model sometimes wraps JSON in ```json fences.
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("`\n ")

    try:
        parsed = json.loads(text)
        scores = parsed.get("scores", [])
    except Exception:
        log.warning("Reranker returned non-JSON: %s", text[:200])
        return candidates[: _top_k()]

    by_id: dict[int, int] = {}
    for entry in scores:
        try:
            by_id[int(entry["id"])] = int(entry["score"])
        except (KeyError, TypeError, ValueError):
            continue

    # Pair each candidate with its rerank score (default 0 if missing).
    rescored = []
    for i, (chunk, emb_score) in enumerate(capped):
        rescored.append(((chunk, emb_score), by_id.get(i, 0)))

    # Sort descending by reranker score; embedding score breaks ties so
    # the original order is preserved when the model can't differentiate.
    rescored.sort(key=lambda x: (x[1], x[0][1]), reverse=True)
    return [pair for pair, _ in rescored[: _top_k()]]
