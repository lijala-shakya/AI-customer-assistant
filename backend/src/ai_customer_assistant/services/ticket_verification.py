"""One-time email verification for support-ticket requests."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlencode
from uuid import UUID

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.ticket_agent.ticket_agent import create_ticket
from agents.ticket_agent.types import PendingTicket, Ticket
from agents.ticket_agent.validation import validate_email
from db.models import Ticket as TicketRecord
from db.models import TicketVerification

logger = logging.getLogger(__name__)


class EmailSettings(BaseSettings):
    """SMTP and public URL settings, supplied through the environment."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_public_url: str = "http://localhost:8000"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    smtp_use_tls: bool = True
    ticket_verification_ttl_minutes: int = 30


class EmailDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedTicket:
    ticket: Ticket
    thread_id: str


class SMTPEmailSender:
    def __init__(self, settings: EmailSettings) -> None:
        self.settings = settings

    async def send_verification(self, *, recipient: str, token: str) -> None:
        s = self.settings
        if not s.smtp_host or not s.smtp_from_email:
            raise EmailDeliveryError(
                "Email is not configured. Set SMTP_HOST and SMTP_FROM_EMAIL."
            )
        url = f"{s.app_public_url.rstrip('/')}/tickets/verify?{urlencode({'token': token})}"
        message = EmailMessage()
        message["Subject"] = "Verify your email to create a support ticket"
        message["From"] = s.smtp_from_email
        message["To"] = recipient
        message.set_content(
            "Click the link below to verify your email address and create your "
            f"support ticket. This link expires in {s.ticket_verification_ttl_minutes} minutes.\n\n{url}"
        )

        def _send() -> None:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
                if s.smtp_use_tls:
                    smtp.starttls()
                if s.smtp_username:
                    smtp.login(s.smtp_username, s.smtp_password or "")
                smtp.send_message(message)

        await asyncio.to_thread(_send)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TicketVerificationService:
    """Persists pending requests and consumes each verification token once."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], sender: SMTPEmailSender) -> None:
        self._session_factory = session_factory
        self._sender = sender
        self._ttl = timedelta(minutes=sender.settings.ticket_verification_ttl_minutes)

    async def request(self, pending: PendingTicket, email: str, *, thread_id: str) -> str:
        normalized_email = validate_email(email)
        token = secrets.token_urlsafe(32)
        async with self._session_factory() as session:
            session.add(TicketVerification(
                token_hash=_token_hash(token), thread_id=thread_id, email=normalized_email,
                query=pending.query, expires_at=datetime.now(timezone.utc) + self._ttl,
            ))
            await session.commit()
        try:
            await self._sender.send_verification(recipient=normalized_email, token=token)
        except Exception:
            logger.exception("Unable to send ticket verification email")
            raise
        return normalized_email

    async def verify(self, token: str) -> VerifiedTicket | None:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            result = await session.execute(
                select(TicketVerification)
                .where(TicketVerification.token_hash == _token_hash(token))
                .with_for_update()
            )
            request = result.scalar_one_or_none()
            if request is None or request.used_at is not None or request.expires_at < now:
                return None
            ticket = create_ticket(PendingTicket(query=request.query), request.email)
            session.add(TicketRecord(
                ticket_id=UUID(ticket.ticket_id), email=ticket.email, query=ticket.query,
                priority=ticket.priority, status="OPEN",
            ))
            request.used_at = now
            await session.commit()
            return VerifiedTicket(ticket=ticket, thread_id=request.thread_id)
