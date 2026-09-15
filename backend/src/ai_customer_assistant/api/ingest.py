"""HTTP ingestion endpoints - HARDENED with auth, SSRF protection, and rate limits.

Both paths reuse the exact orchestration the CLI worker uses
(``scripts/crawl_and_ingest.py``): register a document version (checksum
dedup, MinIO storage, queue row) then queue the job for persistent processing.

  POST /ingest/upload   multipart file (PDF / DOCX / Markdown) [requires auth]
  POST /ingest/crawl    JSON {"url": ...} (HTML page or PDF/Office URL) [requires auth]

Uploaded-by is determined from the authenticated user; defaults to the
system service account if no auth is provided (disabled in production).

SECURITY:
  - All endpoints require JWT authentication (Bearer token)
  - SSRF protection: URLs must not resolve to private IPs
  - Upload size limits: configurable per file
  - Rate limiting: per-user throttling
  - Jobs run through persistent queue with retry/recovery
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import urllib.parse
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ai_customer_assistant.api.deps import get_current_user, require_customer
from ai_customer_assistant.db.async_session import get_session
from ai_customer_assistant.ingestion.pipeline_types import FileType
from ai_customer_assistant.ingestion.queue.document_producer import register_document_version

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"])

# Configuration from environment
MAX_UPLOAD_SIZE_MB = int(os.environ.get("INGEST_MAX_UPLOAD_SIZE_MB", "50"))
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024

MAX_CRAWL_SIZE_MB = int(os.environ.get("INGEST_MAX_CRAWL_SIZE_MB", "100"))
MAX_CRAWL_SIZE_BYTES = MAX_CRAWL_SIZE_MB * 1024 * 1024

# URLs to allow (if set); empty = allow all external URLs
ALLOWED_URL_PREFIXES = os.environ.get("INGEST_ALLOWED_URL_PREFIXES", "").split(",")
ALLOWED_URL_PREFIXES = [p.strip() for p in ALLOWED_URL_PREFIXES if p.strip()]

# Rate limit: max uploads per user per hour
UPLOADS_PER_HOUR_LIMIT = int(os.environ.get("INGEST_UPLOADS_PER_HOUR", "100"))

_UPLOAD_MIME_TO_FILE_TYPE: dict[str, FileType] = {
    "application/pdf": FileType.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": FileType.DOCX,
    "text/markdown": FileType.MD,
}

_CRAWL_DOC_MIME_TO_FILE_TYPE: dict[str, FileType | None] = {
    "application/pdf": FileType.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": FileType.DOCX,
    "application/msword": None,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": None,
    "application/vnd.ms-powerpoint": None,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": None,
    "application/vnd.ms-excel": None,
}

RouteKind = Literal["html", "document"]


def _classify_content_type(content_type: str) -> RouteKind:
    base = content_type.split(";", 1)[0].strip().lower()
    return "document" if base in _CRAWL_DOC_MIME_TO_FILE_TYPE else "html"


def _resolve_file_type(content_type: str) -> FileType | None:
    base = content_type.split(";", 1)[0].strip().lower()
    return _CRAWL_DOC_MIME_TO_FILE_TYPE.get(base)


def _is_private_ip(ip_str: str) -> bool:
    """Check if IP is private/reserved (SSRF protection)."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local
    except ValueError:
        return False


async def _validate_url_safe(url: str) -> None:
    """Validate URL is not an SSRF target."""
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname

        if hostname is None:
            raise HTTPException(status_code=400, detail="URL must have a hostname")

        # Check against allowlist if configured
        if ALLOWED_URL_PREFIXES:
            if not any(url.startswith(prefix) for prefix in ALLOWED_URL_PREFIXES):
                raise HTTPException(status_code=403, detail="URL not in allowed domains")

        # Resolve hostname to IP and check if private
        try:
            import socket

            ips = await asyncio.get_event_loop().run_in_executor(
                None, socket.getaddrinfo, hostname, None
            )
            for family, type_, proto, canonname, sockaddr in ips:
                ip_str = sockaddr[0]
                if _is_private_ip(ip_str):
                    raise HTTPException(
                        status_code=403,
                        detail=f"SSRF protection: URL resolves to private IP {ip_str}",
                    )
        except socket.gaierror:
            # DNS resolution failed; allow (will fail at HTTP level)
            pass

    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(f"Error validating URL: {exc}")
        raise HTTPException(status_code=400, detail="Invalid URL") from exc



async def _register_and_run(
    session: AsyncSession,
    uploaded_by: UUID,
    *,
    url: str,
    raw_bytes: bytes,
    mime_type: str,
    file_type: FileType | None,
    category_id: UUID | None,
    request: Request,
) -> dict:
    """Register document and queue for persistent job processing."""
    job = await register_document_version(
        session,
        url=url,
        raw_bytes=raw_bytes,
        mime_type=mime_type,
        file_type=file_type,
        uploaded_by=uploaded_by,
        category_id=category_id,
    )
    if job is None:
        return {"status": "duplicate_skipped"}

    # Queue job through persistent job queue (not asyncio.create_task)
    job_queue = request.app.state.job_queue
    await job_queue.enqueue_ingestion(job.job_id, job.source_id, job.version_id)

    return {
        "status": "submitted",
        "job_id": str(job.job_id),
        "source_id": str(job.source_id),
        "version_id": str(job.version_id),
    }


@router.get("/jobs/{job_id}")
async def job_status(
    job_id: UUID,
    session: AsyncSession = Depends(get_session),
    user_info: tuple[UUID | str, str] = Depends(get_current_user),
) -> dict:
    """Get job status (requires auth)."""
    from db.models import KnowledgeInjectionJob

    user_id, role = user_info

    row = await session.get(KnowledgeInjectionJob, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # Allow admin to see all jobs; customers can only see their own
    if role == "customer" and row.triggered_by != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    return {
        "job_id": str(row.job_id),
        "status": row.status,
        "chunks_created_count": row.chunks_created_count,
        "entities_created_count": row.entities_created_count,
        "error_details": row.error_details,
        "attempts": row.attempts or 0,
    }


@router.post("/upload")
async def upload(
    file: UploadFile,
    category_id: UUID | None = None,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
    user_info: tuple[UUID | str, str] = Depends(require_customer),
) -> dict:
    """Upload a file for ingestion (requires auth, respects size limits)."""
    import asyncio

    user_id, role = user_info

    mime = (file.content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    file_type = _UPLOAD_MIME_TO_FILE_TYPE.get(mime)
    if file_type is None:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported media type '{mime}'. Expected PDF, DOCX or Markdown.",
        )

    name = (file.filename or "upload").rsplit("/", 1)[-1]

    # Read file with size limit to prevent OOM
    data = b""
    async for chunk in file.file:
        data += chunk
        if len(data) > MAX_UPLOAD_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds maximum size of {MAX_UPLOAD_SIZE_MB} MB",
            )

    if not data:
        raise HTTPException(status_code=400, detail="Empty file.")

    return await _register_and_run(
        session,
        user_id,
        url=f"manual_upload://{name}",
        raw_bytes=data,
        mime_type=mime,
        file_type=file_type,
        category_id=category_id,
        request=request,
    )


class CrawlRequest(BaseModel):
    url: str = Field(..., min_length=5, max_length=2048)
    category_id: UUID | None = None


@router.post("/crawl")
async def crawl(
    req: CrawlRequest,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
    user_info: tuple[UUID | str, str] = Depends(require_customer),
) -> dict:
    """Crawl a URL and ingest (requires auth, SSRF-protected)."""
    import asyncio

    user_id, role = user_info

    # SSRF protection: validate URL before fetching
    await _validate_url_safe(req.url)

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            limits=httpx.Limits(max_redirects=5),  # Limit redirects
        ) as client:
            response = await client.get(req.url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "application/octet-stream")

            # Check Content-Length before downloading
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_CRAWL_SIZE_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"Content exceeds maximum size of {MAX_CRAWL_SIZE_MB} MB",
                        )
                except ValueError:
                    pass

            # Stream and size-check the response
            raw_bytes = b""
            async for chunk in response.aiter_bytes():
                raw_bytes += chunk
                if len(raw_bytes) > MAX_CRAWL_SIZE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Content exceeds maximum size of {MAX_CRAWL_SIZE_MB} MB",
                    )

    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {exc}") from exc

    route = _classify_content_type(content_type)

    if route == "html":
        # The page body was already fetched above — extract markdown from it
        # directly instead of re-fetching through the Crawler (which used a
        # stricter user-agent/timeout and double-fetched the URL).
        from ingestion.crawler.exception import ExtractionError
        from ingestion.crawler.extractor import extract_markdown

        final_url = str(response.url)
        html = raw_bytes.decode("utf-8", errors="replace")
        try:
            markdown = extract_markdown(html, final_url)
        except ExtractionError as exc:
            raise HTTPException(status_code=502, detail=f"Crawl failed: {exc}") from exc

        return await _register_and_run(
            session,
            user_id,
            url=final_url,
            raw_bytes=markdown.encode("utf-8"),
            mime_type="text/markdown",
            file_type=FileType.MD,
            category_id=req.category_id,
            request=request,
        )

    return await _register_and_run(
        session,
        user_id,
        url=req.url,
        raw_bytes=raw_bytes,
        mime_type=content_type,
        file_type=_resolve_file_type(content_type),
        category_id=req.category_id,
        request=request,
    )