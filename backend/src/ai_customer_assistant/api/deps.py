"""FastAPI dependency injection for auth and rate limiting."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ai_customer_assistant.auth.jwt import TokenManager, TokenSettings

# Global token manager (initialized at startup)
_token_manager: TokenManager | None = None


def init_token_manager() -> None:
    """Initialize the global token manager (call from FastAPI lifespan)."""
    global _token_manager
    _token_manager = TokenManager(TokenSettings())


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
) -> tuple[UUID | str, Literal["admin", "customer"]]:
    """Validate JWT bearer token and return (user_id, role)."""
    if _token_manager is None:
        raise HTTPException(status_code=500, detail="Auth not initialized")

    token = credentials.credentials
    payload = _token_manager.verify_token(token)

    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    return UUID(payload.user_id), payload.role


async def require_admin(
    user_info: tuple[UUID | str, Literal["admin", "customer"]] = Depends(get_current_user),
) -> UUID | str:
    """Enforce admin role. Returns user_id."""
    user_id, role = user_info
    if role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user_id


async def require_customer(
    user_info: tuple[UUID | str, Literal["admin", "customer"]] = Depends(get_current_user),
) -> UUID | str:
    """Enforce customer role or higher. Returns user_id."""
    user_id, role = user_info
    # Both admin and customer can access customer endpoints
    if role not in ("admin", "customer"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Customer access required")
    return user_id


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
) -> tuple[UUID | str, Literal["admin", "customer"]] | None:
    """Optional auth: returns user info if token present and valid, else None."""
    if credentials is None:
        return None

    if _token_manager is None:
        return None

    token = credentials.credentials
    payload = _token_manager.verify_token(token)
    if payload is None:
        return None

    return UUID(payload.user_id), payload.role


# Rate limiting counter (in-memory; replace with Redis in production)
_request_counts: dict[str, list[float]] = {}


def _get_ip_key(request_id: str, window_seconds: int = 60) -> str:
    """Simple sliding window rate limit checker."""
    import time

    now = time.time()
    if request_id not in _request_counts:
        _request_counts[request_id] = []

    # Remove old timestamps outside the window
    _request_counts[request_id] = [t for t in _request_counts[request_id] if now - t < window_seconds]

    return request_id


async def check_rate_limit(
    request_id: str,
    max_requests: int = 100,
    window_seconds: int = 60,
) -> None:
    """Raise 429 if rate limit exceeded."""
    import time

    now = time.time()
    key = _get_ip_key(request_id, window_seconds)

    if len(_request_counts[key]) >= max_requests:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")

    _request_counts[key].append(now)
