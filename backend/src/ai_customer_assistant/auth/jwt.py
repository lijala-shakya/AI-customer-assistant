"""JWT authentication and token management (security hardening)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

import jwt
from pydantic import BaseModel, Field


class TokenSettings(BaseModel):
    """JWT token configuration from environment."""

    secret_key: str = Field(default_factory=lambda: os.environ.get("JWT_SECRET_KEY", "change-me-in-production"))
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440  # 24 hours
    refresh_token_expire_days: int = 30

    def __post_init__(self):
        if self.secret_key == "change-me-in-production":
            import warnings

            warnings.warn("JWT_SECRET_KEY not set; using insecure default", RuntimeWarning, stacklevel=2)


class TokenPayload(BaseModel):
    """Decoded JWT token claims."""

    user_id: UUID | str
    role: Literal["admin", "customer"]
    scope: str = "api"  # e.g., "api", "ingest"
    iat: int = Field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp()))
    exp: int = 0

    def is_expired(self) -> bool:
        now = int(datetime.now(timezone.utc).timestamp())
        return now > self.exp


class TokenManager:
    """Create and validate JWT tokens."""

    def __init__(self, settings: TokenSettings | None = None):
        self.settings = settings or TokenSettings()

    def create_access_token(
        self, user_id: UUID | str, role: Literal["admin", "customer"], expires_delta: timedelta | None = None
    ) -> str:
        """Create a signed access token."""
        if expires_delta is None:
            expires_delta = timedelta(minutes=self.settings.access_token_expire_minutes)

        now = datetime.now(timezone.utc)
        expire = now + expires_delta

        payload = TokenPayload(
            user_id=str(user_id),
            role=role,
            scope="api",
            iat=int(now.timestamp()),
            exp=int(expire.timestamp()),
        )

        return jwt.encode(payload.model_dump(), self.settings.secret_key, algorithm=self.settings.algorithm)

    def verify_token(self, token: str) -> TokenPayload | None:
        """Verify and decode a token. Returns None if invalid/expired."""
        try:
            payload = jwt.decode(token, self.settings.secret_key, algorithms=[self.settings.algorithm])
            return TokenPayload(**payload)
        except (jwt.InvalidTokenError, jwt.ExpiredSignatureError, ValueError):
            return None

    def create_refresh_token(self, user_id: UUID | str) -> str:
        """Create a longer-lived refresh token."""
        expires_delta = timedelta(days=self.settings.refresh_token_expire_days)
        now = datetime.now(timezone.utc)
        expire = now + expires_delta

        payload = TokenPayload(
            user_id=str(user_id),
            role="customer",
            scope="refresh",
            iat=int(now.timestamp()),
            exp=int(expire.timestamp()),
        )

        return jwt.encode(payload.model_dump(), self.settings.secret_key, algorithm=self.settings.algorithm)
