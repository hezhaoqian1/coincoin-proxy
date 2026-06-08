from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .media_store import media_artifact_storage_path
from .models import MediaArtifact
from .proxy import authenticate_user


router = APIRouter(prefix="/v1/media-artifacts", tags=["media-artifacts"])


def _serialize_artifact(item: MediaArtifact) -> dict:
    return {
        "id": item.id,
        "type": item.media_type,
        "media_type": item.media_type,
        "endpoint": item.endpoint,
        "model": item.model,
        "provider_model": item.provider_model,
        "status": item.status,
        "url": item.url,
        "thumbnail_url": item.thumbnail_url,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "upstream_request_id": item.upstream_request_id,
        "route_reason": item.route_reason,
        "cost_cents": int(item.cost_cents or 0),
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
    }


@router.get("")
async def list_media_artifacts(
    request: Request,
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
    media_type: str | None = None,
):
    user = await authenticate_user(request, db)
    limit = max(1, min(int(limit or 50), 100))
    offset = max(0, int(offset or 0))

    conditions = [MediaArtifact.user_id == user.id]
    if media_type in {"image", "video"}:
        conditions.append(MediaArtifact.media_type == media_type)
    where = and_(*conditions)

    total = (
        await db.execute(select(func.count()).select_from(MediaArtifact).where(where))
    ).scalar() or 0
    result = await db.execute(
        select(MediaArtifact)
        .where(where)
        .order_by(MediaArtifact.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    items = result.scalars().all()
    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "data": [_serialize_artifact(item) for item in items],
    }


@router.get("/{artifact_id}/content")
async def get_media_artifact_content(
    artifact_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await authenticate_user(request, db)
    item = (
        await db.execute(
            select(MediaArtifact).where(MediaArtifact.id == artifact_id, MediaArtifact.user_id == user.id)
        )
    ).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="media artifact not found")

    try:
        metadata = json.loads(item.metadata_json or "{}")
    except Exception:
        metadata = {}
    storage_name = str(metadata.get("storage_name") or "").strip()
    path = media_artifact_storage_path(storage_name)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="media artifact content not found")

    content_type = str(metadata.get("content_type") or "application/octet-stream").strip()
    return FileResponse(path, media_type=content_type, headers={"cache-control": "private, max-age=86400"})
