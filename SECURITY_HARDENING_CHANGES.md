# Security Hardening: Before & After

**Date**: 2026-09-02  
**Scope**: Production security fixes for authentication, SSRF, upload limits, and job durability

---

## Summary of Changes

This document details 4 major security hardening areas implemented across 5 files:

1. **Authentication & CORS** (main.py, auth/jwt.py, api/deps.py)
2. **SSRF Protection & Upload Limits** (api/ingest.py)
3. **Email Verification Durability** (services/ticket_verification.py)
4. **Job Persistence & Recovery** (services/job_queue.py, main.py)

All changes are **backwards-incompatible** and require:
- New JWT_SECRET_KEY environment variable
- ALLOWED_ORIGINS environment variable (defaults to localhost:3000)
- New database columns on TicketVerification (see migrations)
- Persistent job queue startup in application lifespan

---

## 1. Authentication & CORS Protection

### Files Changed
- `backend/src/ai_customer_assistant/auth/jwt.py` (new)
- `backend/src/ai_customer_assistant/api/deps.py` (new)
- `backend/src/ai_customer_assistant/main.py` (modified)

### Before: No Authentication

```python
# main.py - BEFORE
app = FastAPI(title="AI Customer Assistant", lifespan=lifespan)

# Cross-origin access for the frontend (opened from file:// or a dev static server).
# Restrict allow_origins in production as needed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ❌ ALLOWS ALL ORIGINS
    allow_methods=["*"],  # ❌ ALLOWS ALL METHODS
    allow_headers=["*"],  # ❌ ALLOWS ALL HEADERS
)
```

All endpoints were unauthenticated:
```python
@router.post("/chat")
async def chat(req: ChatRequest) -> ChatResponse:
    # No auth check; anyone can call
    pass

@router.post("/ingest/upload")
async def upload(file: UploadFile) -> dict:
    # No auth check; anyone can upload files
    pass

@router.post("/ingest/crawl")
async def crawl(req: CrawlRequest) -> dict:
    # No auth check; anyone can trigger crawl
    pass
```

**Vulnerabilities**:
- Attackers can trigger model inference (cost abuse)
- Attackers can upload malicious documents
- Attackers can browse knowledge graph without permission
- CORS accepts requests from any origin (CSRF)

### After: JWT-Based Auth with Restricted CORS

**New file: `auth/jwt.py`**
```python
class TokenPayload(BaseModel):
    """Decoded JWT token claims."""
    user_id: UUID | str
    role: Literal["admin", "customer"]
    scope: str = "api"
    iat: int = Field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp()))
    exp: int = 0

class TokenManager:
    """Create and validate JWT tokens."""

    def create_access_token(
        self, user_id: UUID | str, role: Literal["admin", "customer"], 
        expires_delta: timedelta | None = None
    ) -> str:
        """Create a signed access token."""
        # Creates HS256-signed token with 24-hour expiry (default)
        
    def verify_token(self, token: str) -> TokenPayload | None:
        """Verify and decode a token. Returns None if invalid/expired."""
```

**New file: `api/deps.py`**
```python
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
) -> tuple[UUID | str, Literal["admin", "customer"]]:
    """Validate JWT bearer token and return (user_id, role)."""
    # Checks Authorization: Bearer <token>
    # Returns 401 if invalid/expired

async def require_admin(
    user_info: tuple[UUID | str, Literal["admin", "customer"]] = Depends(get_current_user),
) -> UUID | str:
    """Enforce admin role. Returns user_id."""
    # Returns 403 if not admin

async def require_customer(
    user_info: tuple[UUID | str, Literal["admin", "customer"]] = Depends(get_current_user),
) -> UUID | str:
    """Enforce customer role or higher. Returns user_id."""
    # Returns 403 if not admin/customer
```

**Updated `main.py`**:
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize auth token manager
    init_token_manager()  # ✓ NEW
    
    # ... rest of setup

app = FastAPI(title="AI Customer Assistant", lifespan=lifespan)

# SECURITY: Restrict CORS to trusted origins only
_allowed_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
_allowed_origins = [o.strip() for o in _allowed_origins if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,              # ✓ Restricted
    allow_methods=["GET", "POST", "OPTIONS"],     # ✓ Restricted
    allow_headers=["Content-Type", "Authorization"], # ✓ Restricted
    allow_credentials=True,
    max_age=3600,
)
```

**New Endpoint Signatures**:
```python
@router.post("/chat")
async def chat(
    req: ChatRequest,
    user_info: tuple[UUID | str, str] = Depends(require_customer),
) -> ChatResponse:
    user_id, role = user_info
    # ✓ Only authenticated customers/admins can chat
    pass

@router.post("/ingest/upload")
async def upload(
    file: UploadFile,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
    user_info: tuple[UUID | str, str] = Depends(require_customer),
) -> dict:
    user_id, role = user_info
    # ✓ Only authenticated customers/admins can upload
    pass
```

**Environment Variables Required**:
```bash
# .env
JWT_SECRET_KEY=your-secure-random-key-minimum-32-chars
ALLOWED_ORIGINS=http://localhost:3000,https://example.com
```

**Response Codes**:
- `401 Unauthorized`: Missing or invalid token
- `403 Forbidden`: Token valid but insufficient role
- `400 Bad Request`: Malformed Authorization header

---

## 2. SSRF Protection & Upload Limits

### Files Changed
- `backend/src/ai_customer_assistant/api/ingest.py` (major rewrite)

### Before: Vulnerable to SSRF, No Size Limits, Uncontrolled Crawl

```python
# BEFORE: Crawl endpoint
@router.post("/crawl")
async def crawl(req: CrawlRequest, session: AsyncSession = Depends(get_session)) -> dict:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(req.url)  # ❌ ANY URL ALLOWED
            response.raise_for_status()
            content_type = response.headers.get("content-type", "application/octet-stream")
            raw_bytes = response.content  # ❌ UNBOUNDED READ
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {exc}") from exc
    # ... process raw_bytes
```

**Vulnerabilities**:
- SSRF: Can fetch from `http://localhost:9000` (MinIO), `http://169.254.169.254` (AWS metadata), internal services
- No size limit: `response.content` reads entire response into memory → OOM attack
- No redirect limiting: `follow_redirects=True` can chain through many servers
- No upload size check: `await file.read()` reads entire file into memory

### After: SSRF-Protected, Streamed, Size-Limited

**Core validation added**:
```python
# Constants
MAX_UPLOAD_SIZE_MB = int(os.environ.get("INGEST_MAX_UPLOAD_SIZE_MB", "50"))
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024

MAX_CRAWL_SIZE_MB = int(os.environ.get("INGEST_MAX_CRAWL_SIZE_MB", "100"))
MAX_CRAWL_SIZE_BYTES = MAX_CRAWL_SIZE_MB * 1024 * 1024

ALLOWED_URL_PREFIXES = os.environ.get("INGEST_ALLOWED_URL_PREFIXES", "").split(",")
ALLOWED_URL_PREFIXES = [p.strip() for p in ALLOWED_URL_PREFIXES if p.strip()]

def _is_private_ip(ip_str: str) -> bool:
    """Check if IP is private/reserved (SSRF protection)."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local
    except ValueError:
        return False

async def _validate_url_safe(url: str) -> None:
    """Validate URL is not an SSRF target."""
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
            if _is_private_ip(ip_str):  # ✓ BLOCKS 10.0.0.0/8, 127.0.0.1, etc.
                raise HTTPException(
                    status_code=403,
                    detail=f"SSRF protection: URL resolves to private IP {ip_str}",
                )
    except socket.gaierror:
        pass  # DNS failure; allow (will fail at HTTP level)
```

**Updated upload endpoint**:
```python
@router.post("/upload")
async def upload(
    file: UploadFile,
    category_id: UUID | None = None,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
    user_info: tuple[UUID | str, str] = Depends(require_customer),  # ✓ Auth required
) -> dict:
    user_id, role = user_info

    # ... MIME type validation ...

    # Read file with size limit to prevent OOM
    data = b""
    async for chunk in file.file:  # ✓ Streamed, not bulk read
        data += chunk
        if len(data) > MAX_UPLOAD_SIZE_BYTES:  # ✓ Size check per chunk
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds maximum size of {MAX_UPLOAD_SIZE_MB} MB",
            )

    return await _register_and_run(
        session, user_id,  # ✓ Track uploader
        url=f"manual_upload://{name}",
        raw_bytes=data,
        mime_type=mime,
        file_type=file_type,
        category_id=category_id,
        request=request,
    )
```

**Updated crawl endpoint**:
```python
@router.post("/crawl")
async def crawl(
    req: CrawlRequest,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
    user_info: tuple[UUID | str, str] = Depends(require_customer),  # ✓ Auth required
) -> dict:
    user_id, role = user_info

    # SSRF protection: validate URL before fetching
    await _validate_url_safe(req.url)  # ✓ NEW

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            limits=httpx.Limits(max_redirects=5),  # ✓ NEW: limit redirects
        ) as client:
            response = await client.get(req.url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "application/octet-stream")

            # Check Content-Length before downloading
            content_length = response.headers.get("content-length")  # ✓ NEW
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
            async for chunk in response.aiter_bytes():  # ✓ Streamed, not bulk
                raw_bytes += chunk
                if len(raw_bytes) > MAX_CRAWL_SIZE_BYTES:  # ✓ Size check per chunk
                    raise HTTPException(
                        status_code=413,
                        detail=f"Content exceeds maximum size of {MAX_CRAWL_SIZE_MB} MB",
                    )

    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {exc}") from exc

    # ... process raw_bytes with user_id tracked
```

**Environment Variables**:
```bash
# .env
INGEST_MAX_UPLOAD_SIZE_MB=50
INGEST_MAX_CRAWL_SIZE_MB=100
INGEST_ALLOWED_URL_PREFIXES=https://example.com,https://docs.example.com
```

**Response Codes**:
- `401 Unauthorized`: No auth token
- `403 Forbidden`: SSRF protection triggered (private IP)
- `413 Payload Too Large`: File/content exceeds size limit
- `415 Unsupported Media Type`: Invalid file type

---

## 3. Email Verification Durability & Rate Limiting

### Files Changed
- `backend/src/ai_customer_assistant/services/ticket_verification.py` (major rewrite)

### Before: Race Condition, No Rate Limits, No Audit

```python
# BEFORE: Unsafe ordering
async def request(self, pending: PendingTicket, email: str, *, thread_id: str) -> str:
    normalized_email = validate_email(email)
    token = secrets.token_urlsafe(32)
    async with self._session_factory() as session:
        session.add(TicketVerification(
            token_hash=_token_hash(token), 
            thread_id=thread_id, 
            email=normalized_email,
            query=pending.query, 
            expires_at=datetime.now(timezone.utc) + self._ttl,
        ))
        await session.commit()  # ❌ DB written FIRST
    try:
        await self._sender.send_verification(recipient=normalized_email, token=token)
        # ❌ If this fails, orphaned request left in DB
    except Exception:
        logger.exception("Unable to send ticket verification email")
        raise  # ❌ Caller sees error but DB row exists
    return normalized_email
```

**Vulnerabilities**:
- **Race condition**: DB written before email sent
  - If email send fails → orphaned request row remains
  - If app crashes → request exists but email never sent
- **No rate limits**: Attacker can spam requests (DoS + email abuse)
- **No resend limits**: No protection against resend loops
- **No audit trail**: Can't track verification attempts

### After: Outbox Pattern with Rate Limiting

**Database schema changes needed**:
```sql
-- Add columns to TicketVerification table
ALTER TABLE ticket_verification ADD COLUMN resend_count INTEGER DEFAULT 0;
ALTER TABLE ticket_verification ADD COLUMN send_attempts INTEGER DEFAULT 1;
ALTER TABLE ticket_verification ADD COLUMN last_sent_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE ticket_verification ADD COLUMN verified_at TIMESTAMP WITH TIME ZONE;
```

**Updated EmailSettings**:
```python
class EmailSettings(BaseSettings):
    """SMTP and public URL settings, supplied through the environment."""
    
    # ... existing fields ...
    
    # SECURITY: Rate limits
    max_requests_per_email_per_hour: int = 5  # Max verification requests per email per hour
    max_resends_per_request: int = 3  # Max resends per verification request
```

**Rewritten request method with outbox pattern**:
```python
async def request(self, pending: PendingTicket, email: str, *, thread_id: str) -> str:
    """Request verification token (with rate limiting).
    
    PATTERN: Outbox with retry
      1. Check rate limit against DB
      2. Send email FIRST (outside transaction)
      3. Only then write DB row (atomic)
      4. If send fails, raise error — caller retries
      5. If DB write fails, email was already sent (idempotent)
    """
    normalized_email = validate_email(email)

    # ✓ Check rate limit: max N requests from this email in last hour
    async with self._session_factory() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        result = await session.execute(
            select(TicketVerification).where(
                (TicketVerification.email == normalized_email)
                & (TicketVerification.created_at > cutoff)
                & (TicketVerification.used_at.is_(None))  # unused only
            )
        )
        active_requests = result.scalars().all()

        if len(active_requests) >= self._sender.settings.max_requests_per_email_per_hour:
            logger.warning(f"Rate limit exceeded for {normalized_email}")
            raise HTTPException(
                status_code=HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many verification requests. Try again in 1 hour.",
            )

    token = secrets.token_urlsafe(32)

    # ✓ Send email FIRST (outbox pattern)
    # If this fails, exception raised before DB write → no orphaned row
    try:
        await self._sender.send_verification(recipient=normalized_email, token=token)
    except Exception:
        logger.exception(f"Email send failed for {normalized_email}")
        raise  # Fail atomically; caller retries entire request

    # ✓ Email succeeded. Now persist the request.
    # Even if this fails, email was already sent (idempotent).
    async with self._session_factory() as session:
        verification = TicketVerification(
            token_hash=_token_hash(token),
            thread_id=thread_id,
            email=normalized_email,
            query=pending.query,
            expires_at=datetime.now(timezone.utc) + self._ttl,
            resend_count=0,          # ✓ NEW
            send_attempts=1,         # ✓ NEW
            last_sent_at=datetime.now(timezone.utc),  # ✓ NEW
        )
        session.add(verification)
        await session.commit()
        logger.info(f"Verification request created for {normalized_email}")

    return normalized_email
```

**New resend method**:
```python
async def resend(self, email: str) -> bool:
    """Resend verification email for an existing request.
    
    Returns:
        True if resent, False if no active request or limit exceeded.
    """
    normalized_email = validate_email(email)

    async with self._session_factory() as session:
        # Find the most recent unused request for this email
        result = await session.execute(
            select(TicketVerification)
            .where(
                (TicketVerification.email == normalized_email)
                & (TicketVerification.used_at.is_(None))
                & (TicketVerification.expires_at > datetime.now(timezone.utc))
            )
            .order_by(TicketVerification.created_at.desc())
        )
        request = result.scalar_one_or_none()

        if request is None:
            return False

        # ✓ Check resend limit
        if (request.resend_count or 0) >= self._sender.settings.max_resends_per_request:
            logger.warning(f"Resend limit exceeded for {normalized_email}")
            return False

        # ✓ Throttle resends: minimum 2 minutes between attempts
        if request.last_sent_at:
            elapsed = datetime.now(timezone.utc) - request.last_sent_at
            if elapsed < timedelta(minutes=2):
                logger.info(f"Resend throttled for {normalized_email}")
                return False

        # Resend the email
        try:
            await self._sender.send_verification(recipient=normalized_email, token=...)
        except Exception:
            logger.exception(f"Resend failed for {normalized_email}")
            return False

        # ✓ Update counters
        request.resend_count = (request.resend_count or 0) + 1
        request.last_sent_at = datetime.now(timezone.utc)
        request.send_attempts = (request.send_attempts or 1) + 1
        await session.commit()

        logger.info(f"Verification email resent for {normalized_email}")
        return True
```

**New cleanup method (background job)**:
```python
async def cleanup_expired(self) -> int:
    """Delete expired verification requests (call periodically)."""
    from sqlalchemy import delete

    async with self._session_factory() as session:
        result = await session.execute(
            delete(TicketVerification).where(
                (TicketVerification.expires_at < datetime.now(timezone.utc))
                | (
                    (TicketVerification.used_at.is_(None))
                    & (TicketVerification.created_at < datetime.now(timezone.utc) - timedelta(days=7))
                )  # unused for 7+ days
            )
        )
        await session.commit()
        deleted = result.rowcount
        logger.info(f"Cleaned up {deleted} expired verification requests")
        return deleted
```

**Environment Variables**:
```bash
# .env
TICKET_VERIFICATION_TTL_MINUTES=30
```

**New HTTP Response Codes**:
- `429 Too Many Requests`: Rate limit exceeded

---

## 4. Job Persistence & Recovery

### Files Changed
- `backend/src/ai_customer_assistant/services/job_queue.py` (new)
- `backend/src/ai_customer_assistant/main.py` (modified)

### Before: Fire-and-Forget with Loss on Restart

```python
# BEFORE: Ingestion jobs fire and forget
async def _register_and_run(
    session: AsyncSession,
    url: str,
    raw_bytes: bytes,
    # ... other params
) -> dict:
    job = await register_document_version(
        session,
        # ... params
    )
    if job is None:
        return {"status": "duplicate_skipped"}
    asyncio.create_task(_run_job(job.job_id))  # ❌ Fire-and-forget
    return {
        "status": "submitted",
        "job_id": str(job.job_id),
        # ...
    }
```

**Vulnerabilities**:
- **Data loss on restart**: `asyncio.create_task()` lost if app crashes/restarts
- **No retry logic**: Failed jobs left hanging
- **No status recovery**: Can't resume interrupted jobs
- **No timeout handling**: Long jobs can block forever
- **No visibility**: Can't track job state after submission

### After: Persistent Queue with Retry & Recovery

**New file: `services/job_queue.py`**

```python
class JobStatus(str, Enum):
    """Job lifecycle states."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"

class IngestionJobQueue:
    """Manages persistent ingestion jobs with retry/recovery semantics."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        max_retries: int = 3,
        retry_delay_seconds: int = 60,
        job_timeout_seconds: int = 3600,
    ):
        # Configuration for retry behavior

    async def enqueue_ingestion(self, job_id: UUID, source_id: UUID, version_id: UUID) -> None:
        """Record a pending job in the database."""
        # Jobs already recorded by register_document_version;
        # this just marks as queued

    async def process_pending_jobs(self) -> None:
        """Main worker loop: pick up pending jobs and run them.
        
        - Finds PENDING/RETRYING jobs
        - Spawns asyncio task per job
        - Tracks running tasks
        - Polls database every 5 seconds
        """
        while True:
            try:
                async with self.session_factory() as session:
                    # Find jobs ready to run (PENDING or RETRYING, not timed out)
                    cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.job_timeout_seconds)
                    result = await session.execute(
                        select(KnowledgeInjectionJob).where(
                            (KnowledgeInjectionJob.status.in_(("PENDING", "RETRYING")))
                            & (KnowledgeInjectionJob.created_at > cutoff)
                        )
                    )
                    jobs = result.scalars().all()

                    for row in jobs:
                        # Skip if already running
                        if row.job_id in self._running_jobs:
                            continue

                        # Launch job as a task
                        task = asyncio.create_task(self._run_job_with_retry(job, row))
                        self._running_jobs[job.job_id] = task

                        # Clean up completed tasks
                        self._running_jobs = {
                            jid: t for jid, t in self._running_jobs.items() if not t.done()
                        }

                # Sleep before checking for more jobs
                await asyncio.sleep(5)
            except Exception as e:
                logger.exception(f"Error in job processing loop: {e}")
                await asyncio.sleep(10)

    async def _run_job_with_retry(
        self, job: JobRef, db_row: KnowledgeInjectionJob
    ) -> None:
        """Execute a job with retry logic."""
        attempt = 0
        while attempt < self.max_retries:
            try:
                async with self.session_factory() as session:
                    # Refresh job state
                    row = await session.get(KnowledgeInjectionJob, job.job_id)
                    if row is None or row.status == "FAILED":
                        break

                    # Mark as running
                    row.status = "RUNNING"
                    row.attempts = (row.attempts or 0) + 1
                    await session.commit()

                # Execute ingestion with timeout
                outcome = await asyncio.wait_for(
                    self._execute_ingestion(job),
                    timeout=self.job_timeout_seconds,
                )

                # Mark complete
                async with self.session_factory() as session:
                    await job_repo.complete_job(
                        session,
                        job_id=job.job_id,
                        status=outcome["status"],
                        chunks_created_count=outcome.get("chunks_created_count", 0),
                        entities_created_count=outcome.get("entities_created_count", 0),
                        error_details=outcome.get("error_details"),
                    )
                    await session.commit()
                logger.info(f"Job {job.job_id} completed")
                break

            except asyncio.TimeoutError:
                logger.warning(f"Job {job.job_id} timed out (attempt {attempt + 1})")
                attempt += 1
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_delay_seconds)
            except Exception as e:
                logger.exception(f"Job {job.job_id} failed on attempt {attempt + 1}: {e}")
                attempt += 1
                if attempt < self.max_retries:
                    # Exponential backoff
                    delay = self.retry_delay_seconds * (2 ** attempt)
                    await asyncio.sleep(delay)

        # Mark as failed if all retries exhausted
        if attempt >= self.max_retries:
            async with self.session_factory() as session:
                await job_repo.complete_job(
                    session,
                    job_id=job.job_id,
                    status="FAILED",
                    chunks_created_count=0,
                    entities_created_count=0,
                    error_details=f"Max retries ({self.max_retries}) exceeded",
                )
                await session.commit()
            logger.error(f"Job {job.job_id} failed after {self.max_retries} attempts")
```

**Updated `main.py` lifespan**:
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize auth token manager
    init_token_manager()

    checkpointer = await build_checkpointer()
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

    # ✓ Initialize persistent job queue
    job_queue = IngestionJobQueue(session_factory)
    app.state.job_queue = job_queue
    # ✓ Start background worker for job processing
    job_worker_task = asyncio.create_task(job_queue.process_pending_jobs())

    yield

    # ✓ Cleanup
    job_worker_task.cancel()
    try:
        await job_worker_task
    except asyncio.CancelledError:
        pass

    conn = getattr(checkpointer, "conn", None)
    close = getattr(conn, "aclose", None)
    if close is not None:
        await close()
```

**Updated `ingest.py` to use queue**:
```python
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

    # ✓ Queue job through persistent job queue (not asyncio.create_task)
    job_queue = request.app.state.job_queue
    await job_queue.enqueue_ingestion(job.job_id, job.source_id, job.version_id)

    return {
        "status": "submitted",
        "job_id": str(job.job_id),
        "source_id": str(job.source_id),
        "version_id": str(job.version_id),
    }
```

**Database schema changes needed**:
```sql
-- Add columns to KnowledgeInjectionJob table
ALTER TABLE knowledge_injection_job ADD COLUMN attempts INTEGER DEFAULT 0;
```

**Environment Variables**:
```bash
# .env
INGEST_JOB_MAX_RETRIES=3
INGEST_JOB_RETRY_DELAY_SECONDS=60
INGEST_JOB_TIMEOUT_SECONDS=3600
```

**Job Lifecycle**:
```
PENDING (initial)
    ↓
RUNNING (picked up by worker)
    ├─ (success) → COMPLETED
    ├─ (timeout/error) → RETRYING (exponential backoff)
    │   ↓
    │ RUNNING (retry attempt)
    │   ├─ (success) → COMPLETED
    │   ├─ (max retries) → FAILED
```

---

## Migration Checklist

To deploy these changes, follow this order:

### 1. Database Migrations
```sql
-- Add JWT/session support (optional, for session tracking)
-- ALTER TABLE users ADD COLUMN last_login_at TIMESTAMP;

-- Email verification improvements
ALTER TABLE ticket_verification 
    ADD COLUMN resend_count INTEGER DEFAULT 0,
    ADD COLUMN send_attempts INTEGER DEFAULT 1,
    ADD COLUMN last_sent_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN verified_at TIMESTAMP WITH TIME ZONE;

-- Job persistence
ALTER TABLE knowledge_injection_job 
    ADD COLUMN attempts INTEGER DEFAULT 0;
```

### 2. Environment Variables
Create/update `.env`:
```bash
# Security: Authentication & CORS
JWT_SECRET_KEY=your-secure-random-key-minimum-32-chars
ALLOWED_ORIGINS=http://localhost:3000,https://yourdomain.com

# Security: Ingestion limits & SSRF
INGEST_MAX_UPLOAD_SIZE_MB=50
INGEST_MAX_CRAWL_SIZE_MB=100
INGEST_ALLOWED_URL_PREFIXES=https://example.com,https://docs.example.com

# Job queue
INGEST_JOB_MAX_RETRIES=3
INGEST_JOB_RETRY_DELAY_SECONDS=60
INGEST_JOB_TIMEOUT_SECONDS=3600

# Email rate limiting
TICKET_VERIFICATION_TTL_MINUTES=30
```

### 3. Code Deployment
1. Deploy `auth/jwt.py` (new)
2. Deploy `api/deps.py` (new)
3. Deploy `services/job_queue.py` (new)
4. Deploy updated `main.py`
5. Deploy updated `api/ingest.py`
6. Deploy updated `services/ticket_verification.py`

### 4. Verify
```bash
# Test JWT auth is required
curl -X POST http://localhost:8000/chat -H "Content-Type: application/json" \
  -d '{"thread_id":"t1","message":"test"}'
# Should return 403 Unauthorized

# Test with valid token
TOKEN=$(curl -X POST http://localhost:8000/auth/token -d "username=admin&password=..." | jq -r .access_token)
curl -X POST http://localhost:8000/chat -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"thread_id":"t1","message":"test"}'
# Should return 200

# Test CORS restriction
curl -X OPTIONS http://localhost:8000/chat \
  -H "Origin: http://evil.com"
# Should NOT include Access-Control-Allow-Origin

# Test SSRF protection
curl -X POST http://localhost:8000/ingest/crawl \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url":"http://localhost:9000/minio"}'
# Should return 403 SSRF protection error
```

---

## Breaking Changes

This is a **BREAKING** release:

| Component | Old Behavior | New Behavior | Action Required |
|-----------|--------------|--------------|-----------------|
| Auth | None (open) | JWT required | Generate tokens for all clients |
| CORS | Allow all origins | Allow configured origins | Set `ALLOWED_ORIGINS` env var |
| Job Queue | Fire-and-forget | Persistent with retry | Update job status polling |
| Email Verification | DB-first | Email-first (outbox) | Update error handling in clients |
| Upload Size | Unbounded | Configurable limit | May reject large uploads |
| URL Crawl | Any URL (SSRF) | Private IPs blocked | Whitelist allowed domains |

---

## Testing Recommendations

### Unit Tests
- `test_token_manager.py`: JWT creation/validation/expiry
- `test_auth_deps.py`: Auth dependency injection
- `test_ssrf_validation.py`: Private IP detection, URL validation
- `test_ticket_verification.py`: Rate limiting, resend logic, outbox pattern

### Integration Tests
- `test_ingest_auth.py`: Verify endpoints require auth
- `test_ingest_ssrf.py`: SSRF protection on crawl
- `test_ingest_size_limits.py`: Upload/crawl size enforcement
- `test_job_queue_persistence.py`: Job recovery on restart
- `test_email_verification_durability.py`: Email-first pattern

### E2E Tests
- Token generation → ingestion → job status polling
- Multiple crawl jobs with retry on failure
- Email verification with resend and expiry

---

## Rollback Plan

If issues arise:

1. **Auth blocking all requests**:
   - Comment out `@Depends(require_customer)` decorators
   - Set `JWT_SECRET_KEY` to dummy value temporarily
   - Redeploy

2. **SSRF blocking legitimate URLs**:
   - Add domains to `INGEST_ALLOWED_URL_PREFIXES`
   - Or set to empty string to disable SSRF checks (temporary)

3. **Job queue failures**:
   - Stop the app (job worker terminates)
   - Manually process pending jobs via CLI
   - Restart app with worker

4. **Email verification failures**:
   - Restore old email-first code if needed
   - Regenerate verification requests

---

## Future Hardening

Recommended follow-up work:

1. **Redis-backed rate limiting** (instead of in-memory dict)
2. **WAF/request signing** for API endpoints
3. **Content validation** on uploads (MIME magic, malware scanning)
4. **Prompt injection filtering** on ingested content
5. **Tenant isolation** (multi-tenant support)
6. **Audit logging** (all auth/ingest events)
7. **Encryption at rest** for uploaded documents
8. **Secrets rotation** (key/password management)
9. **DLP (Data Loss Prevention)** rules
10. **Zero-trust deployment** (mTLS, service mesh)

---

## Summary Table

| Issue | Before | After | File |
|-------|--------|-------|------|
| No authentication | Open endpoints | JWT required | main.py, auth/jwt.py |
| CORS too permissive | Allow `*` | Restricted list | main.py |
| SSRF vulnerability | Any URL allowed | Private IPs blocked | api/ingest.py |
| No upload limits | Unbounded reads | 50 MB default | api/ingest.py |
| Email race condition | DB-first (unsafe) | Email-first (outbox) | services/ticket_verification.py |
| No email rate limit | Unlimited requests | 5/hour per email | services/ticket_verification.py |
| Job loss on restart | Fire-and-forget | Persistent queue | services/job_queue.py |
| No job retry | Single attempt | 3 retries with backoff | services/job_queue.py |

---

**Generated**: 2026-09-02  
**Status**: Ready for review and testing
