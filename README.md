# Filenergy

A small file host that turns into a searchable, askable knowledge base. Drop
PDFs, DOCX, Markdown or text files in and Filenergy extracts the text,
embeds it, and lets you ask Claude questions about your library.

Built on Flask 3 + SQLite, [Voyage](https://www.voyageai.com/) for embeddings,
and the Anthropic Claude API for answers.

## Features

- Multi-file upload with progress (jQuery-File-Upload).
- Per-user file lists, public/private toggle, hash-based share URLs.
- Automatic indexing on upload: PDF / DOCX / TXT / MD / CSV / JSON / HTML.
- Semantic search over your files via Voyage embeddings (cosine similarity).
- `/ask` chat page: retrieval-augmented Q&A with Claude (adaptive thinking,
  prompt caching, streaming).
- Cited answers — every response links back to the source files.

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Configure

Copy the variables below into a `.env` file at the repo root (the app loads
it via `python-dotenv`):

```env
# Required for /ask. Get one at https://console.anthropic.com/
ANTHROPIC_API_KEY=sk-ant-...

# Required for indexing + retrieval. Get one at https://www.voyageai.com/
VOYAGE_API_KEY=pa-...

# Optional
FILENERGY_SECRET_KEY=change-me-in-production
FILENERGY_DB_PATH=filenergy.db
FILENERGY_UPLOAD_DIR=files
CLAUDE_MODEL=claude-opus-4-7        # or claude-sonnet-4-6 for ~3x cheaper
VOYAGE_EMBED_MODEL=voyage-3-lite
```

If `ANTHROPIC_API_KEY` and `VOYAGE_API_KEY` are not set, the app still runs as
a basic file host — only `/ask` and indexing are disabled.

### 3. Run

```bash
python manage.py
```

Open http://localhost:5000.

### Optional commands

```bash
python manage.py create-superuser admin@example.com 'a-good-password'
python manage.py reindex   # rebuild embeddings for all existing files
```

## How it works

```
upload  ─►  text extraction  ─►  chunk (1200 chars, 150 overlap)  ─►  Voyage embed
                                                                          │
                                                                          ▼
                                                                     SQLite (chunk.embedding)

ask  ─►  Voyage embed (query)  ─►  cosine top-k  ─►  Claude (RAG)  ─►  answer + sources
```

- Embeddings are stored as JSON-encoded float arrays directly in SQLite. Fine
  up to ~10K chunks per user; swap in pgvector or sqlite-vec if you grow past
  that.
- The system prompt is sent with `cache_control: {type: "ephemeral"}` so it
  gets reused across requests when it crosses Anthropic's caching threshold.
- Adaptive thinking is enabled — Claude decides when reasoning helps.

## Roadmap

- Server-Sent Events on `/ask` for token-by-token streaming to the browser.
- Migrate embeddings storage to sqlite-vec for sub-second retrieval at scale.
- Multi-user workspaces (shared libraries, ACLs).
- Stripe billing per workspace, with metered usage on the Claude side.
