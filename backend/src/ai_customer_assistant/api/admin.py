"""Admin dashboard endpoints for operational visibility.

Provides read-only access to:
- Knowledge sources + version status
- Ingestion job queue status
- Graph statistics (entity/relation counts)
- Support tickets
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_customer_assistant.db.async_session import get_session
from ai_customer_assistant.db.models import (
    Entity,
    KnowledgeInjectionJob,
    KnowledgeSource,
    KnowledgeSourceVersion,
    Relation,
    Ticket,
)

router = APIRouter(prefix="/admin", tags=["admin"])


# ============================================================================
# Response Models
# ============================================================================


class KnowledgeSourceResponse(BaseModel):
    source_id: UUID
    source_name: str | None
    source_type: str
    current_version_status: str | None
    current_version_number: int | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class IngestionJobResponse(BaseModel):
    job_id: UUID
    source_id: UUID
    version_id: UUID | None
    job_type: str
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    chunks_created_count: int | None
    entities_created_count: int | None
    error_details: str | None

    class Config:
        from_attributes = True


class GraphStatsResponse(BaseModel):
    entities_total: int
    relations_total: int
    entities_by_type: list[dict[str, str | int]] = Field(default_factory=list)
    relations_by_type: list[dict[str, str | int]] = Field(default_factory=list)


class TicketResponse(BaseModel):
    ticket_id: UUID
    email: str
    query: str
    priority: str | None
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/knowledge-sources", response_model=list[KnowledgeSourceResponse])
async def list_knowledge_sources(
    session: AsyncSession = Depends(get_session),
) -> list[KnowledgeSourceResponse]:
    """List all knowledge sources with their current version status."""
    stmt = select(KnowledgeSource).order_by(KnowledgeSource.created_at.desc())
    result = await session.execute(stmt)
    sources = result.scalars().all()

    response = []
    for source in sources:
        current_version = source.current_version
        response.append(
            KnowledgeSourceResponse(
                source_id=source.source_id,
                source_name=source.source_name,
                source_type=source.source_type,
                current_version_status=current_version.status if current_version else None,
                current_version_number=current_version.version_number if current_version else None,
                created_at=source.created_at,
                updated_at=source.updated_at,
            )
        )
    return response


@router.get("/jobs", response_model=list[IngestionJobResponse])
async def list_ingestion_jobs(
    session: AsyncSession = Depends(get_session),
    limit: int = 50,
) -> list[IngestionJobResponse]:
    """List recent ingestion jobs with status and error details."""
    stmt = (
        select(KnowledgeInjectionJob)
        .order_by(KnowledgeInjectionJob.completed_at.desc().nulls_last())
        .limit(limit)
    )
    result = await session.execute(stmt)
    jobs = result.scalars().all()

    return [
        IngestionJobResponse.from_orm(job)
        for job in jobs
    ]


@router.get("/stats", response_model=GraphStatsResponse)
async def graph_stats(
    session: AsyncSession = Depends(get_session),
) -> GraphStatsResponse:
    """Get knowledge graph statistics: entity and relation counts."""
    # Total entities
    entities_count_stmt = select(func.count(Entity.id))
    entities_total = await session.scalar(entities_count_stmt) or 0

    # Total relations
    relations_count_stmt = select(func.count(Relation.id))
    relations_total = await session.scalar(relations_count_stmt) or 0

    # Entities by type
    entities_by_type_stmt = (
        select(
            Entity.entity_type,
            func.count(Entity.id).label("count"),
        )
        .group_by(Entity.entity_type)
        .order_by(func.count(Entity.id).desc())
    )
    entities_by_type_result = await session.execute(entities_by_type_stmt)
    entities_by_type = [
        {"type": row[0], "count": row[1]}
        for row in entities_by_type_result
    ]

    # Relations by type
    relations_by_type_stmt = (
        select(
            Relation.relation_type,
            func.count(Relation.id).label("count"),
        )
        .group_by(Relation.relation_type)
        .order_by(func.count(Relation.id).desc())
    )
    relations_by_type_result = await session.execute(relations_by_type_stmt)
    relations_by_type = [
        {"type": row[0], "count": row[1]}
        for row in relations_by_type_result
    ]

    return GraphStatsResponse(
        entities_total=entities_total,
        relations_total=relations_total,
        entities_by_type=entities_by_type,
        relations_by_type=relations_by_type,
    )


@router.get("/tickets", response_model=list[TicketResponse])
async def list_tickets(
    session: AsyncSession = Depends(get_session),
    limit: int = 50,
) -> list[TicketResponse]:
    """List support tickets."""
    stmt = (
        select(Ticket)
        .order_by(Ticket.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    tickets = result.scalars().all()

    return [
        TicketResponse.from_orm(ticket)
        for ticket in tickets
    ]
