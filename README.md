# Filenergy

A private file host that turns into a searchable, askable knowledge base.
Drop PDFs, DOCX, Markdown or text files in and Filenergy extracts the text,
embeds it, and lets you chat with your library — citations included.

Built on Flask 3 + SQLite, [Voyage](https://www.voyageai.com/) for
embeddings, and the Anthropic Claude API for answers.

## Features

- **Multi-file upload** with per-user isolation and hash-based share URLs.
- **Automatic indexing** on upload: PDF, DOCX, TXT, Markdown, CSV, JSON, HTML
  and any text/* MIME type. Status badges in the file list show
  indexed / pending / error per row, and you can re-index any file with one
  click.
- **Semantic search** built on Voyage embeddings + cosine similarity.
- **Streaming chat** at `/ask` — Server-Sent Events deliver tokens as
  Claude generates them. Markdown is rendered live; sources link back to
  the originals.
- **Multi-turn conversations** persisted to SQLite. Pick threads up where
  you left off, with the last 12 turns sent back to the model for context.
- **Rate limiting** on `/ask` (sliding window, DB-backed) so a runaway
  client can't drain your API budget.
- **Event log** for every meaningful action: upload, download, index,
  question, answer, login, rate-limit hits. Doubles as the rate-limiter's
  audit trail and as the substrate for billing.
- **Public share toggle** per file. Owners get private by default; downloads
  enforce ownership unless the file is explicitly public.
- **97% test coverage** (165 tests, pytest + pytest-cov).

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Configure

Copy these into a `.env` at the repo root:

```env
# Required for /ask. https://console.anthropic.com/
ANTHROPIC_API_KEY=sk-ant-...

# Required for indexing + retrieval. https://www.voyageai.com/
VOYAGE_API_KEY=pa-...

# Optional
FILENERGY_SECRET_KEY=change-me-in-production
FILENERGY_DB_PATH=filenergy.db
FILENERGY_UPLOAD_DIR=files
CLAUDE_MODEL=claude-opus-4-7
VOYAGE_EMBED_MODEL=voyage-3-lite
FILENERGY_ASK_RATE_LIMIT=30           # requests
FILENERGY_ASK_RATE_WINDOW=60          # seconds
```

Without API keys the app still runs as a basic file host — only `/ask` and
indexing are disabled, with clear UI banners.

### 3. Run

```bash
python manage.py
```

http://localhost:5000

### CLI commands

```bash
python manage.py create-superuser admin@example.com 'a-good-password'
python manage.py reindex
```

## Architecture

```
upload  →  text extraction  →  chunk (1200 chars, 150 overlap)  →  Voyage embed
                                                                       │
                                                                       ▼
                                                                  SQLite

ask     →  Voyage embed (query)  →  cosine top-K  →  Claude (RAG)  →  SSE stream  →  browser
                                                                          │
                                                                          ▼
                                                                  message + sources
                                                                  persisted to thread
```

- Embeddings live as JSON-encoded float arrays in SQLite. Cheap up to
  ~10K chunks per user. Swap in `sqlite-vec` or pgvector when you grow past
  that.
- `/ask` and `/ask/stream` both use prompt caching on the system prompt and
  adaptive thinking. The streaming endpoint is wired to a `text_stream`
  iterator from the Anthropic SDK and proxied verbatim to the browser as
  SSE.
- Every meaningful action emits an `Event` row. The rate limiter just
  counts those events in a sliding window, so the audit trail and the
  quota check are the same source of truth.

## Project layout

```
filenergy/
├── settings.py             # env-based config, no hardcoded secrets
├── __init__.py             # app, db, login_manager
├── middleware.py           # before_request wiring
├── admin.py                # Flask-Admin views (superuser only)
├── models/                 # User, File, Chunk, Conversation, Message, Event
├── services/
│   ├── base.py             # generic SQLAlchemy service
│   ├── user.py
│   ├── file.py
│   ├── extraction.py       # pdf/docx/txt extractors + chunker
│   ├── embeddings.py       # Voyage client + cosine search
│   ├── chat.py             # RAG + Anthropic streaming
│   ├── conversations.py    # threads + messages
│   ├── events.py           # analytics + audit
│   └── rate_limit.py       # DB-backed sliding window
├── views/                  # blueprints: index, user, file, ask
├── templates/              # bootstrap-3 templates
└── static/

tests/                      # pytest suite, 97% coverage
```

## Testing

```bash
pip install pytest pytest-cov
python -m pytest                  # run the suite
python -m pytest --cov            # with coverage report
```

## Roadmap

- Replace JSON embeddings with `sqlite-vec` for sub-second retrieval at
  100K+ chunks.
- Stripe integration: per-workspace metered billing keyed off the event log.
- API keys for programmatic upload + ask.
- Workspaces (shared libraries, ACLs, invitations).
- Background indexing queue (RQ) so large PDFs don't block the upload
  response.
