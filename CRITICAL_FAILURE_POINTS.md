# Critical Failure Points & System Dependencies

**Date**: 2026-09-02  
**Purpose**: Document single points of failure and critical dependencies that could break the entire system

---

## TL;DR - The Single Critical Point

**If PostgreSQL database goes down → entire application stops.**

Everything depends on the database connection established during startup. There is no graceful degradation.

---

## Critical Dependency Chain

```
Application Startup
    ↓
lifespan() hook executes
    ├─ init_token_manager()
    ├─ build_checkpointer()
    ├─ get_async_session_factory()  ← CRITICAL: Creates DB connection
    ├─ build_chat_service()
    ├─ TicketVerificationService(session_factory)
    └─ IngestionJobQueue(session_factory)  ← Depends on DB
    
    ↓ (If any step fails, app fails to start)
```

---

## 1. **Database Connection Failure** (MOST CRITICAL)

### What Breaks
- ❌ Token validation (auth endpoints)
- ❌ Chat service (knowledge graph queries)
- ❌ Ingestion pipeline (document registration)
- ❌ Email verification (rate limit checks)
- ❌ Job queue (status tracking)

### Vulnerable Code

**File**: `main.py`
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # If DB is unreachable, app crashes here:
    session_factory = get_async_session_factory()  # ← CRASH POINT
    
    app.state.ticket_verification_service = TicketVerificationService(
        session_factory,  # ← Needs working DB
        SMTPEmailSender(EmailSettings())
    )
    # ... more initialization
```

**File**: `db/session.py` (underlying)
```python
def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    # Creates connection pool to PostgreSQL
    # If POSTGRES_HOST/POSTGRES_PASSWORD/POSTGRES_PORT are wrong → crash
    engine = create_async_engine(database_url())  # ← Can fail here
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
```

### How It Breaks
```bash
# Scenario: PostgreSQL is down or credentials are wrong
$ uv run uvicorn src.ai_customer_assistant.main:app --reload

ERROR: Application startup failed
sqlalchemy.exc.OperationalError: (asyncpg.exceptions.CannotConnectNowError) 
could not connect to the server: Name or service not known
```

**Result**: App won't start. **All endpoints return 500 or connection refused.**

### Required Environment Variables
```bash
POSTGRES_USER=ai_assistant
POSTGRES_PASSWORD=ai_customer_assistant_password  # ← Must be correct
POSTGRES_DB=ai_customer_assistant
POSTGRES_HOST=postgres                            # ← Must be reachable
POSTGRES_PORT=5432
```

**Single Touch to Break**:
```bash
# Change in .env:
POSTGRES_HOST=invalid-host
# or
POSTGRES_PASSWORD=wrong-password
```

---

## 2. **JWT Secret Key Missing/Invalid** (CRITICAL)

### What Breaks
- ❌ Token creation for new users
- ❌ Token validation for existing requests
- ❌ Auth endpoints return 500

### Vulnerable Code

**File**: `auth/jwt.py`
```python
class TokenSettings(BaseModel):
    secret_key: str = Field(
        default_factory=lambda: os.environ.get("JWT_SECRET_KEY", "change-me-in-production")
    )
    
    def __post_init__(self):
        if self.secret_key == "change-me-in-production":
            import warnings
            warnings.warn("JWT_SECRET_KEY not set; using insecure default", RuntimeWarning)
```

**File**: `api/deps.py`
```python
def init_token_manager() -> None:
    """Initialize the global token manager (call from FastAPI lifespan)."""
    global _token_manager
    _token_manager = TokenManager(TokenSettings())  # ← Uses JWT_SECRET_KEY
```

### How It Breaks

**Scenario 1: Missing Secret Key**
```bash
# .env is missing JWT_SECRET_KEY line
# App starts but uses insecure default "change-me-in-production"
# Result: Anyone who knows the default can forge tokens
```

**Scenario 2: Wrong Secret After Rotation**
```bash
# You rotate JWT_SECRET_KEY in .env
# Existing tokens become invalid
# Result: All users logged out, need to re-authenticate
```

### Required Environment Variable
```bash
JWT_SECRET_KEY=your-secure-random-key-minimum-32-chars-change-in-production
```

**Single Touch to Break**:
```bash
# Remove from .env:
# JWT_SECRET_KEY=...
# or set to empty:
JWT_SECRET_KEY=
```

**Impact**: All API calls return `401 Unauthorized` or `500 Internal Server Error`

---

## 3. **CORS Configuration Error** (PARTIAL BREAK)

### What Breaks
- ❌ Frontend requests blocked (all endpoints)
- ❌ Users see "CORS policy" errors in browser console
- ✅ API still works (Postman/curl works fine)

### Vulnerable Code

**File**: `main.py`
```python
_allowed_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
_allowed_origins = [o.strip() for o in _allowed_origins if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,  # ← Controls CORS
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)
```

### How It Breaks

**Scenario 1: Empty Origins List**
```bash
# .env:
ALLOWED_ORIGINS=
# Result: Parses to []
# Every frontend request returns: "Access to XMLHttpRequest blocked by CORS policy"
```

**Scenario 2: Wrong Frontend URL**
```bash
# Frontend runs on http://localhost:3001
# But ALLOWED_ORIGINS=http://localhost:3000
# Result: Frontend blocked
```

**Scenario 3: Typo in Domain**
```bash
# Frontend: https://example.com
# ALLOWED_ORIGINS: https://examle.com  ← typo
# Result: CORS blocked
```

### Required Environment Variable
```bash
ALLOWED_ORIGINS=http://localhost:3000,https://yourdomain.com
```

**Single Touch to Break**:
```bash
# In .env:
ALLOWED_ORIGINS=http://wrong-domain.com
```

**Impact**: Frontend can't make API calls (but backend is fine)

---

## 4. **Auth Endpoint Missing** (FUNCTIONAL BREAK)

### What Breaks
- ❌ No way to get JWT tokens
- ❌ No users can authenticate
- ❌ All protected endpoints return `401 Unauthorized`

### Missing Code

**File**: `api/auth.py` (NOT YET CREATED)
```python
# This file doesn't exist yet!
# Without it, clients have no way to get tokens
```

### How It Breaks

**Current State**:
```
1. Frontend tries: POST /auth/token
   ↓
2. Gets 404 Not Found
   ↓
3. Can't get JWT token
   ↓
4. Frontend tries: POST /chat
   ↓
5. Gets 401 Unauthorized (no token)
```

### Solution
Create `api/auth.py` with login endpoint (see Action Items below)

**Impact**: No authentication works at all

---

## 5. **Database Columns Missing** (OPERATIONAL BREAK)

### What Breaks
- ❌ Email verification fails (`resend_count`, `send_attempts`, `last_sent_at` columns missing)
- ❌ Job queue fails (`attempts` column missing)
- ❌ SQLAlchemy insert errors

### Vulnerable Code

**File**: `services/ticket_verification.py`
```python
verification = TicketVerification(
    token_hash=_token_hash(token),
    resend_count=0,          # ← Needs DB column
    send_attempts=1,         # ← Needs DB column
    last_sent_at=datetime.now(timezone.utc),  # ← Needs DB column
    verified_at=None,        # ← Needs DB column
)
session.add(verification)
await session.commit()  # ← CRASH: column "resend_count" of relation "ticket_verification" does not exist
```

### How It Breaks

**Scenario: Migration Not Applied**
```bash
# Created alembic migration file
# But didn't run: alembic upgrade head
# Result: DB schema doesn't have new columns
# SQLAlchemy insert fails with: OperationalError: column does not exist
```

### Required Migration
```bash
cd backend
uv run alembic upgrade head
```

**Impact**: Email verification and job queue operations fail

---

## 6. **Job Queue Not Initialized** (DEGRADATION)

### What Breaks
- ⚠️ Jobs are queued but never processed
- ⚠️ Uploads appear to succeed but documents never get indexed
- ✅ API still works (looks like success to client)

### Vulnerable Code

**File**: `main.py`
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... initialization ...
    
    # If this is missing:
    job_queue = IngestionJobQueue(session_factory)
    app.state.job_queue = job_queue
    job_worker_task = asyncio.create_task(job_queue.process_pending_jobs())
    
    # Result: ingest.py tries to access request.app.state.job_queue
    # Gets AttributeError: 'Starlette' object has no attribute 'job_queue'
```

### How It Breaks

**Scenario: Job Queue Code Removed by Accident**
```python
# In main.py, if you delete:
job_queue = IngestionJobQueue(session_factory)
app.state.job_queue = job_queue

# Then ingest.py fails:
job_queue = request.app.state.job_queue  # ← AttributeError
await job_queue.enqueue_ingestion(...)
```

**Result**: Upload returns `500 Internal Server Error`

---

## 7. **Environment Variable Not Set** (VARIOUS BREAKS)

### Variables and Impact

| Env Var | Missing Value | Result |
|---------|---------------|--------|
| `JWT_SECRET_KEY` | Uses insecure default | Tokens can be forged |
| `ALLOWED_ORIGINS` | Uses default (localhost:3000) | Frontend on different origin blocked |
| `POSTGRES_HOST` | Required, has default | Uses "postgres" (works in Docker, fails locally) |
| `POSTGRES_PASSWORD` | Required, empty string | DB connection fails |
| `INGEST_MAX_UPLOAD_SIZE_MB` | Uses default (50 MB) | Accepts up to 50 MB files |
| `INGEST_ALLOWED_URL_PREFIXES` | Empty string | Allows all URLs (SSRF possible if not validated) |

### Single Touch to Break
```bash
# Delete entire .env file
rm .env
# App crashes on startup with missing required vars
```

---

## System Resilience Matrix

| Component | Failure Mode | Recovery Time | User Impact |
|-----------|--------------|----------------|------------|
| PostgreSQL down | App won't start | Manual restart (mins) | ❌ Total outage |
| JWT_SECRET_KEY wrong | Tokens invalid | Redeploy + reauth (mins) | ❌ All users logged out |
| CORS misconfigured | Frontend blocked | Config update + refresh (secs) | ❌ Frontend broken |
| Auth endpoint missing | No login possible | Deploy auth.py (mins) | ❌ No authentication |
| DB migration not run | Schema mismatch | Run alembic upgrade (secs) | ❌ Ingest/email broken |
| Job queue not init | Jobs not processed | Restart app + redeploy (mins) | ⚠️ Silent failure (looks OK) |
| Upload size limit exceeded | 413 Payload Too Large | Increase config (secs) | ⚠️ Large uploads rejected |
| SSRF allowed URLs | Private IPs accessible | Add whitelist (secs) | ❌ Security breach |

---

## Prevention Checklist

Before deploying to production:

- [ ] PostgreSQL running and reachable (`POSTGRES_HOST`, `POSTGRES_PASSWORD` correct)
- [ ] `JWT_SECRET_KEY` set to strong random value (minimum 32 chars)
- [ ] `JWT_SECRET_KEY` NOT in git repo (in `.env` only, not `.env.example`)
- [ ] `ALLOWED_ORIGINS` includes frontend domain(s)
- [ ] Database migrations applied (`alembic upgrade head` ran successfully)
- [ ] Auth endpoint created (`api/auth.py` deployed)
- [ ] Job queue initialized in `main.py` lifespan
- [ ] All required tables exist with all columns:
  - `ticket_verification`: `resend_count`, `send_attempts`, `last_sent_at`, `verified_at`
  - `knowledge_injection_job`: `attempts`
- [ ] Test authentication: `POST /auth/token` returns 200 with valid token
- [ ] Test protected endpoint: `POST /chat` with token returns 200
- [ ] Test SSRF: `POST /ingest/crawl` with `http://localhost:9000` returns 403
- [ ] Test upload limit: Upload 100 MB file returns 413 (if limit is 50 MB)

---

## Recovery Procedures

### If PostgreSQL Is Down
```bash
# Check connectivity
telnet postgres 5432

# If Docker:
docker-compose up -d postgres

# Verify migration state
docker exec postgres psql -U ai_assistant -d ai_customer_assistant -c "\dt"

# Restart app
uv run uvicorn src.ai_customer_assistant.main:app --reload
```

### If JWT_SECRET_KEY Is Wrong
```bash
# 1. Fix .env
JWT_SECRET_KEY=your-new-secure-key

# 2. Restart app
uv run uvicorn src.ai_customer_assistant.main:app --reload

# 3. All existing tokens are now invalid (users need to re-login)
```

### If CORS Is Broken
```bash
# 1. Check current setting
grep ALLOWED_ORIGINS .env

# 2. Fix .env
ALLOWED_ORIGINS=http://localhost:3000,https://yourdomain.com

# 3. Refresh frontend (no restart needed)
```

### If Auth Endpoint Is Missing
```bash
# 1. Create api/auth.py (see below)
# 2. Update main.py to register router:
from api.auth import router as auth_router
app.include_router(auth_router)

# 3. Restart app
uv run uvicorn src.ai_customer_assistant.main:app --reload
```

### If Database Migration Not Applied
```bash
# 1. Check status
uv run alembic current

# 2. Run upgrade
uv run alembic upgrade head

# 3. Verify
uv run alembic current

# 4. Check table schema
# psql -U ai_assistant -d ai_customer_assistant
# \d ticket_verification
# \d knowledge_injection_job
```

---

## Action Items

### Immediate (Required)
1. [ ] Create `backend/src/ai_customer_assistant/api/auth.py` (see below)
2. [ ] Register auth router in `main.py`
3. [ ] Run `uv run alembic upgrade head`
4. [ ] Set `JWT_SECRET_KEY` in `.env` to strong random value
5. [ ] Test: `POST /auth/token` returns token

### Short Term
1. [ ] Add integration tests for auth failures
2. [ ] Add health check endpoint for monitoring
3. [ ] Document password rotation procedure
4. [ ] Set up monitoring for DB connectivity

### Long Term
1. [ ] Replace in-memory rate limiting with Redis
2. [ ] Add circuit breaker for DB connection
3. [ ] Implement graceful degradation (cache responses)
4. [ ] Add automated backups and recovery testing

---

## Appendix: Create Auth Endpoint

**File**: `backend/src/ai_customer_assistant/api/auth.py` (create this)

```python
"""Authentication endpoints for token generation."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from api.deps import TokenManager, TokenSettings

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Login credentials."""

    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class TokenResponse(BaseModel):
    """JWT token response."""

    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int = 86400  # 24 hours


@router.post("/token", response_model=TokenResponse)
async def login(req: LoginRequest) -> TokenResponse:
    """Generate JWT token for a user.
    
    For now: uses hardcoded demo credentials.
    In production: validate against user database.
    """
    # TODO: Integrate with actual user database
    # For demo purposes only:
    if req.username == "demo" and req.password == "demo":
        token_manager = TokenManager(TokenSettings())
        token = token_manager.create_access_token(
            user_id="00000000-0000-0000-0000-000000000001",
            role="customer",
        )
        return TokenResponse(
            access_token=token,
            token_type="bearer",
            expires_in_seconds=86400,
        )

    if req.username == "admin" and req.password == "admin":
        token_manager = TokenManager(TokenSettings())
        token = token_manager.create_access_token(
            user_id="00000000-0000-0000-0000-000000000000",
            role="admin",
        )
        return TokenResponse(
            access_token=token,
            token_type="bearer",
            expires_in_seconds=86400,
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid username or password",
    )
```

Then in `main.py`, add:
```python
from api.auth import router as auth_router

# ... inside app initialization:
app.include_router(auth_router)  # Add this line
```

---

**Generated**: 2026-09-02  
**Status**: Reference document for production readiness
