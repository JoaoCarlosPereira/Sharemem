"""Admin endpoints for user/group usage analytics dashboard."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from app.database import get_db
from app.models import Group, User
from app.utils.creator_identity import (
    identity_for_hostname,
    resolve_creator_identities_with_db,
)
from app.utils.user_analytics import (
    group_activity_stats,
    recent_user_reads,
    recent_user_writes,
    top_contributors,
    user_activity_stats,
    visible_group_members,
)
from app.utils.legacy_user_deletion import purge_legacy_host_user
from app.utils.machine_resolver import (
    consolidate_group_legacy_members,
    find_legacy_host_user,
)
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func
from sqlalchemy.orm import Session

router = APIRouter(prefix="/admin/analytics", tags=["analytics"])


class GroupAnalyticsSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    member_count: int
    active_members_7d: int
    writes_total: int
    writes_24h: int
    writes_7d: int
    reads_total: int
    reads_24h: int
    reads_7d: int


class UserAnalyticsSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[UUID] = None
    user_id: str
    name: Optional[str] = None
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    group_id: Optional[UUID] = None
    group_name: Optional[str] = None
    created_at: Optional[datetime] = None
    writes_total: int = 0
    writes_24h: int = 0
    writes_7d: int = 0
    reads_total: int = 0
    reads_24h: int = 0
    reads_7d: int = 0
    distinct_memories_read: int = 0
    last_write_at: Optional[datetime] = None
    last_read_at: Optional[datetime] = None
    usage_level: str = "offline"
    offline_days: Optional[int] = None


class UserAnalyticsDetail(UserAnalyticsSummary):
    writes_30d: int = 0
    reads_30d: int = 0
    distinct_projects_written: int = 0
    distinct_projects_read: int = 0
    recent_writes: list[dict] = []
    recent_reads: list[dict] = []


class UserDeleteRequest(BaseModel):
    """Confirmação explícita — deve repetir o hostname exato."""

    confirm: str


class TopContributorItem(BaseModel):
    rank: int
    user_id: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    value: float
    writes: int
    reads: int
    distinct_projects: int = 0


class TopContributorsResponse(BaseModel):
    metric: str
    period: str
    project: Optional[str] = None
    group_id: Optional[str] = None
    items: list[TopContributorItem]


def _get_group_or_404(db: Session, group_id: UUID) -> Group:
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


def _user_summary(
    db: Session,
    user: User,
    *,
    identity_display_name: Optional[str] = None,
    identity_avatar_url: Optional[str] = None,
) -> UserAnalyticsSummary:
    stats = user_activity_stats(db, user.user_id)
    group_name = user.group.name if user.group else None
    return UserAnalyticsSummary(
        id=user.id,
        user_id=user.user_id,
        name=user.name,
        display_name=identity_display_name,
        avatar_url=identity_avatar_url,
        group_id=user.group_id,
        group_name=group_name,
        created_at=user.created_at,
        writes_total=stats["writes_total"],
        writes_24h=stats["writes_24h"],
        writes_7d=stats["writes_7d"],
        reads_total=stats["reads_total"],
        reads_24h=stats["reads_24h"],
        reads_7d=stats["reads_7d"],
        distinct_memories_read=stats["distinct_memories_read"],
        last_write_at=stats["last_write_at"],
        last_read_at=stats["last_read_at"],
        usage_level=stats["usage_level"],
        offline_days=stats["offline_days"],
    )


def _visible_group_members(db: Session, group_id: UUID) -> list[tuple[User, Optional[str]]]:
    """Return person accounts and unpaired legacy hosts with metric hostnames."""
    return visible_group_members(db, group_id)


@router.get("/top-contributors")
def get_top_contributors(
    metric: Literal["writes", "reads", "total"] = "total",
    period: Literal["24h", "7d", "30d", "all"] = "7d",
    group_id: Optional[UUID] = None,
    project: Optional[str] = None,
    limit: int = 10,
    db: Session = Depends(get_db),
) -> dict:
    """Top contributors ranked by write/read activity."""
    if group_id is not None:
        _get_group_or_404(db, group_id)
    payload = top_contributors(
        db,
        metric=metric,
        period=period,
        group_id=group_id,
        project=project.strip() if project else None,
        limit=limit,
    )
    return TopContributorsResponse(**payload).model_dump(mode="json")


@router.get("/groups")
def list_groups_analytics(db: Session = Depends(get_db)) -> dict:
    """List all groups with aggregated usage metrics."""
    groups = db.query(Group).order_by(Group.name).all()
    items: list[dict] = []
    for group in groups:
        stats = group_activity_stats(db, group.id)
        summary = GroupAnalyticsSummary(
            id=group.id,
            name=group.name,
            member_count=stats["member_count"],
            active_members_7d=stats["active_members_7d"],
            writes_total=stats["writes_total"],
            writes_24h=stats["writes_24h"],
            writes_7d=stats["writes_7d"],
            reads_total=stats["reads_total"],
            reads_24h=stats["reads_24h"],
            reads_7d=stats["reads_7d"],
        )
        items.append(summary.model_dump(mode="json"))
    return {"groups": items}


@router.get("/groups/{group_id}")
def get_group_analytics(group_id: UUID, db: Session = Depends(get_db)) -> dict:
    """Group detail with per-member usage stats."""
    group = _get_group_or_404(db, group_id)
    consolidate_group_legacy_members(db, group_id)
    stats = group_activity_stats(db, group.id)
    members = _visible_group_members(db, group_id)
    identities = resolve_creator_identities_with_db(db, (hostname for _, hostname in members))
    member_summaries: list[dict] = []
    for member, hostname in members:
        identity = identity_for_hostname(hostname, identities)
        summary_user = UserAnalyticsSummary(
            id=member.id,
            user_id=hostname,
            name=member.name,
            display_name=(
                identity.display_name
                if identity
                else member.display_name or member.name or member.email or hostname
            ),
            avatar_url=identity.avatar_url if identity else member.avatar_url,
            group_id=member.group_id,
            group_name=member.group.name if member.group else None,
            created_at=member.created_at,
        )
        member_stats = user_activity_stats(db, hostname)
        summary_user = summary_user.model_copy(update=member_stats)
        member_summaries.append(
            summary_user.model_dump(mode="json")
        )
    return {
        "group": GroupAnalyticsSummary(
            id=group.id,
            name=group.name,
            **stats,
        ).model_dump(mode="json"),
        "members": member_summaries,
    }


@router.get("/users/{hostname}")
def get_user_analytics(hostname: str, db: Session = Depends(get_db)) -> dict:
    """Per-user usage profile with recent write/read activity."""
    user = find_legacy_host_user(db, hostname)
    canonical = user.user_id if user is not None else hostname
    stats = user_activity_stats(db, canonical)
    identity = identity_for_hostname(
        canonical,
        resolve_creator_identities_with_db(db, [canonical]),
    )
    if user is not None:
        summary = _user_summary(
            db,
            user,
            identity_display_name=identity.display_name if identity else None,
            identity_avatar_url=identity.avatar_url if identity else None,
        )
    else:
        summary = UserAnalyticsSummary(
            user_id=canonical,
            display_name=identity.display_name if identity else None,
            avatar_url=identity.avatar_url if identity else None,
            writes_total=stats["writes_total"],
            writes_24h=stats["writes_24h"],
            writes_7d=stats["writes_7d"],
            reads_total=stats["reads_total"],
            reads_24h=stats["reads_24h"],
            reads_7d=stats["reads_7d"],
            distinct_memories_read=stats["distinct_memories_read"],
            last_write_at=stats["last_write_at"],
            last_read_at=stats["last_read_at"],
            usage_level=stats["usage_level"],
            offline_days=stats["offline_days"],
        )
    detail = UserAnalyticsDetail(
        **summary.model_dump(),
        writes_30d=stats["writes_30d"],
        reads_30d=stats["reads_30d"],
        distinct_projects_written=stats["distinct_projects_written"],
        distinct_projects_read=stats["distinct_projects_read"],
        recent_writes=recent_user_writes(db, canonical),
        recent_reads=recent_user_reads(db, canonical),
    )
    return detail.model_dump(mode="json")


@router.delete("/users/{hostname}")
def delete_legacy_user(
    hostname: str,
    payload: UserDeleteRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Remove permanentemente um usuário legado (hostname) do catálogo SQL.

    Exige ``confirm`` igual ao hostname. Não apaga vetores no Qdrant.
    """
    if payload.confirm.strip() != hostname.strip():
        raise HTTPException(
            status_code=400,
            detail="confirmação inválida: digite o hostname exato do usuário",
        )
    return purge_legacy_host_user(db, hostname)


@router.get("/overview")
def analytics_overview(db: Session = Depends(get_db)) -> dict:
    """Global summary for the dashboard header."""
    total_groups = db.query(func.count(Group.id)).scalar() or 0
    groups = db.query(Group).all()
    total_users = 0
    active_users_7d = 0
    writes_total = 0
    writes_24h = 0
    writes_7d = 0
    reads_total = 0
    reads_24h = 0
    reads_7d = 0
    for group in groups:
        stats = group_activity_stats(db, group.id)
        total_users += stats["member_count"]
        writes_total += stats["writes_total"]
        writes_24h += stats["writes_24h"]
        writes_7d += stats["writes_7d"]
        reads_total += stats["reads_total"]
        reads_24h += stats["reads_24h"]
        reads_7d += stats["reads_7d"]
        active_users_7d += stats["active_members_7d"]
    return {
        "total_users": total_users,
        "total_groups": total_groups,
        "active_users_7d": active_users_7d,
        "writes_total": writes_total,
        "writes_24h": writes_24h,
        "writes_7d": writes_7d,
        "reads_total": reads_total,
        "reads_24h": reads_24h,
        "reads_7d": reads_7d,
    }
