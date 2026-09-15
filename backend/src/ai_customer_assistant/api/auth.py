"""Authentication endpoints for token generation."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ai_customer_assistant.api.deps import TokenManager, TokenSettings

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
    
    Demo Credentials:
    - username: demo, password: demo → customer role
    - username: admin, password: admin → admin role
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
