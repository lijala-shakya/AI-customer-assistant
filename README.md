# AI Customer Assistant

An end-to-end **multi-agent AI customer-care portal** with hybrid RAG knowledge-base Q&A, support-ticket workflow with human escalation, document ingestion (PDF / DOCX / Markdown / URL crawl), knowledge-graph explorer, and admin dashboard.

Built with **FastAPI + LangGraph + PostgreSQL (pgvector) + MinIO + Tika** on the backend and a **vanilla JS SPA (no build step)** on the frontend. Fully containerized with Docker Compose.

## Features

**Customer portal (chat)**
- Conversational Q&A grounded in the ingested knowledge base, with citations context
- Multi-turn memory via LangGraph checkpointer (server-side, keyed by `thread_id`)
- `interrupt()`-based flows: escalation confirmation + ticket email collection (resume by sending next message with same `thread_id`)
- Thread management (localStorage), typing indicator, retry, `trace_id` debug panel

**Multi-agent system (LangGraph Supervisor)**
- `Supervisor` — intent classification + routing + finalization
- `Knowledge` — hybrid RAG: rewrite → extract → structured / vector / hybrid retrieval → rank → dedupe → grounded LLM answer
- `Safety` — per-sentence embedding groundedness check, escalation gate
- `Ticket` — idempotency-keyed ticket creation (`TicketStore`)

**Document ingestion**
- Upload: PDF, DOCX, Markdown (`POST /ingest/upload`)
- Crawl: single URL or whole site (`POST /ingest/crawl`, `scripts/crawl_and_ingest.py`)
- Pipeline: Tika text extraction → chunking (500 tok / 75 overlap) → BGE embeddings (`BAAI/bge-base-en-v1.5`, 768-dim) → pgvector + EAV graph store → MinIO object storage
- Background `IngestionJobQueue` worker with retries, timeout, deduplication (`duplicate_skipped`)

**Knowledge-graph explorer**
- Unified 2D/3D viewer: search by name / value, entity-type filter, expand, path A→B, neighbors / subgraph, detail panel with typed facts, PNG/JSON export, deep-linking

**Admin / Ops**
- Knowledge sources, ingestion jobs, stats, tickets views
- JWT auth, CORS allowlist, upload/crawl size limits, SSRF prefix allowlist, SMTP ticket verification, rate limiting

## Architecture

```mermaid
flowchart LR
    U([User / Customer]) -->|POST /chat {thread_id, message}| S[Supervisor Agent<br/>classify → route]
    S -->|KNOWLEDGE_QUERY| K[Knowledge Agent<br/>hybrid RAG]
    K --> G[Safety Gate<br/>groundedness check]
    G -->|grounded| A[assemble_response → reply]
    G -->|ungrounded| E[Escalation confirm interrupt]
    E -->|yes| T[Ticket Agent<br/>email interrupt → create]
    S -->|CREATE_TICKET| T
    A --> U

    AD([Admin]) -->|upload / crawl| DIP[Ingestion Pipeline<br/>Tika → chunk → embed]
    DIP -->|indexes| DB[(Postgres + pgvector<br/>+ EAV graph)]
    DIP -->|stores files| M[(MinIO S3)]
    K -->|retrieves| DB
```

**Request flow:** client only sends `{thread_id, message}` — history lives in Postgres checkpointer (falls back to in-memory `MemorySaver` in dev/tests).

Full details: `docs/architecture.md`, `docs/agent implementation and integration.md`, `frontend/frontend_plan.md`.

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI, Uvicorn, Pydantic / pydantic-settings, SQLAlchemy (async + sync), Alembic |
| Agents / LLM | LangGraph, LangChain, LangChain-Groq, Anthropic SDK, Groq model `openai/gpt-oss-120b`, Sentence-Transformers (BGE), Trafilatura |
| Data | PostgreSQL 16 + pgvector, MinIO (S3), Apache Tika (text extraction) |
| Auth / Infra | PyJWT, `python-multipart`, Docker Compose, `uv` package manager, GNU Make |
| Frontend | Vanilla JS ES modules, hash router (`#/chat`, `#/graph`, `#/ingest`, `#/admin`), Cytoscape / force-graph via CDN, no bundler |

## Services (docker-compose.yml)

| Service | Image | Ports | Purpose |
|---|---|---|---|
| `postgres` | `pgvector/pgvector:pg16` | `5433:5432` | App DB: vectors, EAV graph, tickets, jobs, checkpointer |
| `minio` | `minio/minio` | `9000`, `9001` | S3 object store for source documents (`knowledge-documents` bucket) |
| `tika` | `apache/tika:latest-full` | `9998` | Text extraction for PDF/DOCX |
| `backend` | built from `backend/Dockerfile` | `${APP_PORT:-8000}:8000` | FastAPI app + serves `frontend/` same-origin |
| `minio-init` | `minio/mc` | — | One-shot bucket creation |

## Prerequisites

- Docker + Docker Compose
- `uv` (Python package manager)
- GNU Make (Git-Bash `sh` on Windows)
- Groq API key (`GROQ_API_KEY`) for real LLM answers (else deterministic stub)

## Quickstart

```bash
# 1. Check prerequisites, create backend/.env from example
make setup

# 2. Edit backend/.env — at minimum:
#    GROQ_API_KEY, JWT_SECRET_KEY, SMTP_* (for ticket emails)

# 3. Bring up the full stack (postgres + minio + tika + backend)
make up

# 4. Apply DB migrations (host -> localhost:5433)
make migrate

# 5. Ensure default service account exists
make user

# 6. Health check
make smoke
# curl http://127.0.0.1:8000/health -> {"status":"ok"}

# 7. Open the app (served same-origin by backend)
# http://127.0.0.1:8000/#/chat
```

Local dev server (hot reload on port 8002, talks to Docker Postgres on 5433):

```bash
make uvicorn   # or: make run / make backend
make frontend  # opens http://127.0.0.1:8002/#/chat
make graph     # opens http://127.0.0.1:8002/#/graph
```

Stop / rebuild / logs:

```bash
make down
make rebuild
make logs SERVICE=backend
make status
```

## Configuration (.env)

Copy `backend/.env.example` → `backend/.env`. Key settings:

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_HOST/PORT/USER/PASSWORD/DB` | `postgres/5432/ai_assistant/...` (docker) ; host targets use `localhost:5433` | Database connection |
| `MINIO_ENDPOINT/ROOT_USER/ROOT_PASSWORD/BUCKET/SECURE` | `localhost:9000/minioadmin/.../knowledge-documents/false` | S3 document store |
| `TIKA_BASE_URL` | `http://localhost:9998` | Text extraction |
| `APP_PORT` / `APP_PUBLIC_URL` | `8000` | Backend port |
| `GROQ_API_KEY` / `GROQ_MODEL` | `openai/gpt-oss-120b` | Chat classification + knowledge LLM |
| `EAV_MODEL` / `EAV_EXTRACTION_REQUIRED` | | Entity/relation extraction |
| `INGESTION_CHUNK_SIZE_TOKENS/OVERLAP/...` | `500/75/BAAI/bge-base-en-v1.5/768/true` | Chunking + embeddings |
| `JWT_SECRET_KEY` (≥32 chars) / `ALLOWED_ORIGINS` | | Auth + CORS |
| `INGEST_MAX_UPLOAD_SIZE_MB/MAX_CRAWL_SIZE_MB/ALLOWED_URL_PREFIXES` | `50/100/` | Ingestion limits + SSRF guard |
| `INGEST_JOB_MAX_RETRIES/RETRY_DELAY/TIMEOUT` | `3/60/3600` | Job queue |
| `SMTP_HOST/PORT/USERNAME/PASSWORD/FROM_EMAIL/USE_TLS`, `TICKET_VERIFICATION_TTL_MINUTES` | | Ticket email verification |

> Do not copy host-side `POSTGRES_HOST=localhost` / `POSTGRES_PORT=5433` (Makefile defaults) into `backend/.env` — that file holds docker-internal values (`postgres:5432`).

## Usage

### Chat — `/#/chat`

Send `POST /chat {thread_id, message}`. Keep `thread_id` constant per conversation to resume `interrupt()` flows (escalation confirm, email capture).

### Ingest documents

UI: `/#/ingest` (drag-and-drop upload, crawl form).

CLI:

```bash
# Single URL
make ingest URL=https://example.com/docs SITE=n

# Whole site crawl
make ingest URL=https://example.com SITE=y

# Background worker (alternative to in-app queue)
make worker

# Inspect sources
make verify
```

Direct API:

```bash
curl -F "file=@doc.pdf" http://127.0.0.1:8000/ingest/upload
curl -X POST http://127.0.0.1:8000/ingest/crawl \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/page","category_id":null}'
```

### Graph explorer — `/#/graph`

Search, seed/expand nodes, path A→B, filter by entity/relation type, export PNG/JSON.

### Admin — `/#/admin`

Sources / Jobs / Stats / Tickets tabs (requires wired admin endpoints + auth token).

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Liveness → `{status: ok}` |
| `POST` | `/chat` | `{thread_id, message}` → `{thread_id, reply, trace_id}` |
| `POST` | `/auth/login` | JWT login |
| `POST` | `/ingest/upload` | Multipart PDF/DOCX/MD → `{job_id, source_id, version_id}` or `{status: duplicate_skipped}` |
| `POST` | `/ingest/crawl` | `{url, category_id}` → job/source/version ids |
| `GET` | `/graph/search?q=&entity_type=&limit=` | Entity search by name |
| `GET` | `/graph/search_value?q=&limit=` | Entity search by fact value |
| `GET` | `/graph/entities/{id}` | Entity + facts detail |
| `GET` | `/graph/entities/{id}/neighbors?depth=` | Graph fragment |
| `GET` | `/graph/subgraph?entity_id=&depth=` | Subgraph fragment |
| `GET` | `/graph/path?source=&target=&max_depth=` | Path between entities |
| `GET` | `/admin/...` | Sources / jobs / stats / tickets (read-only ops) |

## Testing

```bash
cd backend
uv sync                              # first time
uv run pytest -q                     # full suite (no infra needed: MemorySaver + stub LLM + fake BGE)
uv run pytest tests/services -q      # serving layer
uv run pytest tests/agents -q        # agent packages
uv run pytest tests/db tests/api -q  # checkpointer + HTTP routes
```

Or from repo root: `make test`, `make test-quick` (supervisor wiring only).

Manual supervisor smoke:

```bash
cd backend
uv run python -m ai_customer_assistant.agents.supervisor.cli "Hi there"
uv run python -m ai_customer_assistant.agents.supervisor.cli "Create a ticket" --provider groq --thread-id t1
```

## Project Structure

```
.
├── backend/
│   ├── src/ai_customer_assistant/
│   │   ├── main.py               # FastAPI bootstrap, lifespan, CORS, static mount
│   │   ├── config.py             # pydantic-settings
│   │   ├── api/                  # routes, auth, admin, graph, ingest, deps
│   │   ├── agents/
│   │   │   ├── supervisor/       # classifier, graph, wiring, report, cli
│   │   │   ├── knowledge/        # RAG subgraph, retrieval, providers, rank
│   │   │   ├── safety_agent/     # groundedness.py, report
│   │   │   └── ticket_agent/     # store.py (idempotent), agent
│   │   ├── services/             # chat_service, embeddings (BGE singleton), job_queue, ticket_verification
│   │   ├── ingestion/            # chunking, embedding, storage pipeline
│   │   ├── db/                   # models, session (sync+async), checkpointer
│   │   └── auth/                 # JWT helpers
│   ├── alembic/                  # migrations
│   ├── scripts/                  # entrypoint.sh, run_worker.py, crawl_and_ingest.py
│   ├── tests/                    # contracts, agents, services, db, api, chunk_embed
│   └── Dockerfile
├── frontend/
│   ├── index.html                # app shell + nav
│   └── src/                      # config, api client, router, theme, utils, pages/{chat,graph,ingest,admin}.js
├── docs/                         # architecture, agent integration, ingestion workflow
├── docker-compose.yml
├── Makefile
└── README.md
```

## Security Notes

- CORS restricted via `ALLOWED_ORIGINS`; allowed methods/headers narrowed; JWT required on protected routes.
- Upload/crawl size caps, URL-prefix allowlist (SSRF guard), job retries/timeout, ticket email verification with TTL + rate limiting.
- See `SECURITY_HARDENING_CHANGES.md` and `CRITICAL_FAILURE_POINTS.md` for the full hardening audit.

## Docs & Reports

- `docs/architecture.md` — MVP scope + high-level diagram
- `docs/agent implementation and integration.md` — 6-phase agent build log, request flow, test map
- `frontend/frontend_plan.md` — frontend phases, backend dependencies
- `CHANGELOG.md` / `CHANGE_REPORT.md` — change history
