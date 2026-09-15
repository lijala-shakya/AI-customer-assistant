"""FastAPI app bootstrap (Phase 5, §4.5) - HARDENED with auth & security."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from ai_customer_assistant.api.deps import init_token_manager
from ai_customer_assistant.api.admin import router as admin_router
from ai_customer_assistant.api.auth import router as auth_router
from ai_customer_assistant.api.graph import router as graph_router
from ai_customer_assistant.api.ingest import router as ingest_router
from ai_customer_assistant.api.routes import router
from ai_customer_assistant.db.checkpointer import build_checkpointer
from ai_customer_assistant.db.session import get_async_session_factory
from ai_customer_assistant.services.chat_service import build_chat_service
from ai_customer_assistant.services.embeddings import build_shared_embeddings
from ai_customer_assistant.services.job_queue import IngestionJobQueue
from ai_customer_assistant.services.ticket_verification import EmailSettings, SMTPEmailSender, TicketVerificationService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize auth token manager
    init_token_manager()

    checkpointer = await build_checkpointer()
    # Phase 6 wiring: the real Knowledge graph is compiled only when BOTH the
    # shared BGE instance and the async session factory are passed in.
    shared_embeddings = build_shared_embeddings()
    session_factory = get_async_session_factory()
    app.state.ticket_verification_service = TicketVerificationService(
        session_factory, SMTPEmailSender(EmailSettings())
    )
    service = await build_chat_service(
        checkpointer=checkpointer,
        shared_embeddings=shared_embeddings,
        session_factory=session_factory,
    )
    app.state.chat_service = service

    # Initialize persistent job queue
    job_queue = IngestionJobQueue(session_factory)
    app.state.job_queue = job_queue
    # Start background worker for job processing
    job_worker_task = asyncio.create_task(job_queue.process_pending_jobs())

    yield

    # Cleanup
    job_worker_task.cancel()
    try:
        await job_worker_task
    except asyncio.CancelledError:
        pass

    conn = getattr(checkpointer, "conn", None)
    close = getattr(conn, "aclose", None)
    if close is not None:
        await close()


app = FastAPI(title="AI Customer Assistant", lifespan=lifespan)

# SECURITY: Restrict CORS to trusted origins only. Set ALLOWED_ORIGINS in .env.
# Format: "http://localhost:3000,https://example.com"
_allowed_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
_allowed_origins = [o.strip() for o in _allowed_origins if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "OPTIONS"],  # Restrict HTTP methods
    allow_headers=["Content-Type", "Authorization"],  # Restrict headers
    allow_credentials=True,
    max_age=3600,
)


class _NoCacheMiddleware(BaseHTTPMiddleware):
    """Dev-friendly: force the browser to revalidate static assets every
    request so frontend edits show up without a manual cache purge."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if not response.headers.get("cache-control"):
            response.headers["Cache-Control"] = "no-cache"
        return response


app.add_middleware(_NoCacheMiddleware)

# Chat (registered first — owns POST /chat)
app.include_router(router)
# Authentication (login endpoint)
app.include_router(auth_router)
# Admin dashboard (read-only operational endpoints)
app.include_router(admin_router)
# Read-only knowledge-graph browsing (powers frontend/graph_viewer*.html)
app.include_router(graph_router)
# Document ingestion: multipart upload + URL crawl
app.include_router(ingest_router)
# NOTE: api.chat is NOT registered — its POST /chat collides with router's.
# NOTE: ingestion.storage.api is NOT registered yet — its dependencies still
#       raise NotImplementedError (storage/api.py:62-83).


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# Serve the frontend at the site root so the app runs same-origin:
# http://<host>:<port>/#/chat (frontend/src/config.js uses location.origin).
# Registered last so the API routers above take precedence. FRONTEND_DIR is
# /app/frontend inside the docker image; defaults to the repo's frontend dir.
_FRONTEND_DIR = os.environ.get("FRONTEND_DIR") or str(
    Path(__file__).resolve().parents[3] / "frontend"
)
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")

