"""
Idempotent ticket persistence boundary for the Supervisor adapter.

``create_ticket`` itself is pure and immutable (see ticket_agent.py) — it
cannot dedupe because it has no memory. Deduplication lives here instead, in
the one place the adapter hands a created ``Ticket`` to something stateful.
An ``IdempotentTicketStore`` records every ``(idempotency_key -> Ticket)`` it
has produced; a retried create with the same key returns the previously
created ``Ticket`` instead of booking a second one, so the caller renders the
*existing* ticket's confirmation rather than a fresh row.

The in-memory implementation is the MVP default (same spirit as
``MemorySaver`` for the checkpointer). A durable backend (the ``ticket``
table in ``db/models.py:318``) swaps in at Phase 5 behind the same interface —
the adapter only ever sees the store's ``create_ticket(...)``.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_customer_assistant.agents.ticket_agent.ticket_agent import call as _call
from ai_customer_assistant.agents.ticket_agent.ticket_agent import create_ticket as _build_ticket
from ai_customer_assistant.agents.ticket_agent.types import PendingTicket, Ticket
from ai_customer_assistant.db.models import Ticket as TicketRecord
from ai_customer_assistant.services.ticket_verification import EmailSettings, SMTPEmailSender, TicketVerificationService


class TicketStore:
    """Handles idempotency for ticket creation.

    This is the full ``ticket_ops`` object the Supervisor adapter drives:
    ``call(query)`` on the opening turn and ``create_ticket(...)`` on the
    resume turn, so a ``TicketStore`` instance can be passed directly as
    ``build_supervisor_graph(ticket_ops=...)``.

    ``create_ticket(pending, email, idempotency_key)``:

    - With an ``idempotency_key`` already seen: returns the previously
      created ``Ticket`` (no second creation, no state change).
    - Otherwise: builds a fresh ``Ticket`` and records it, keyed by
      ``idempotency_key`` when one was supplied, plus appends to
      ``rows`` so tests can count created rows.

    ``next_sequence(scope)`` returns the count of tickets already recorded
    under an idempotency-key scope prefix (e.g. a ``thread_id``) — the
    server-derived part of the idempotency key (§2.3).

    ``rows`` is the observable "table" the adapter's persistence step and
    the tests assert against.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, Ticket] = {}
        self.rows: list[Ticket] = []

    def call(self, query: str) -> PendingTicket:
        """Open a ticket for ``query`` — compose so the store is a complete
        ``ticket_ops`` for the Supervisor adapter."""
        return _call(query)

    def next_sequence(self, scope: str) -> int:
        """Number of tickets already created under an idempotency-key
        ``scope`` prefix (for server-derived keys like ``thread_id``)."""
        return sum(1 for key in self._by_key if key.startswith(f"{scope}:"))

    def create_ticket(
        self,
        pending: PendingTicket,
        email: str,
        idempotency_key: str | None = None,
    ) -> Ticket:
        if idempotency_key is not None and idempotency_key in self._by_key:
            return self._by_key[idempotency_key]

        ticket = _build_ticket(pending, email)
        self.rows.append(ticket)
        if idempotency_key is not None:
            self._by_key[idempotency_key] = ticket
        return ticket


class PostgresTicketStore:
    """Ticket persistence adapter used by the running application.

    The ticket agent still owns validation and UUID generation.  This class
    owns the I/O boundary: it writes the completed ticket to PostgreSQL, then
    retains a small per-process idempotency cache for retry protection during
    the current server lifetime.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._by_key: dict[str, Ticket] = {}
        self._verification = TicketVerificationService(
            session_factory, SMTPEmailSender(EmailSettings())
        )

    def call(self, query: str) -> PendingTicket:
        return _call(query)

    async def request_verification(
        self, pending: PendingTicket, email: str, *, thread_id: str
    ) -> str:
        """Email a one-time link; ticket creation happens only after click."""
        return await self._verification.request(pending, email, thread_id=thread_id)

    def next_sequence(self, scope: str) -> int:
        return sum(1 for key in self._by_key if key.startswith(f"{scope}:"))

    async def create_ticket(
        self,
        pending: PendingTicket,
        email: str,
        idempotency_key: str | None = None,
    ) -> Ticket:
        if idempotency_key is not None and idempotency_key in self._by_key:
            return self._by_key[idempotency_key]

        ticket = _build_ticket(pending, email)
        async with self._session_factory() as session:
            session.add(
                TicketRecord(
                    ticket_id=UUID(ticket.ticket_id),
                    email=ticket.email,
                    query=ticket.query,
                    priority=ticket.priority,
                    status="OPEN",
                )
            )
            await session.commit()

        if idempotency_key is not None:
            self._by_key[idempotency_key] = ticket
        return ticket
