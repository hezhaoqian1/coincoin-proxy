from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from datetime import datetime
from pathlib import Path
import re
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import MediaArtifact
from .security import generate_id


logger = logging.getLogger("coincoin.media_store")

_HTTP_PREFIXES = ("http://", "https://")
_DATA_IMAGE_RE = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,(.+)$", re.IGNORECASE | re.DOTALL)
_IMAGE_MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


def _http_url(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    return cleaned if cleaned.startswith(_HTTP_PREFIXES) else ""


def _media_artifact_storage_root() -> Path:
    root = Path(settings.media_artifact_storage_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def media_artifact_storage_path(storage_name: str) -> Path | None:
    name = Path(str(storage_name or "")).name
    if not name or name != str(storage_name or ""):
        return None
    return _media_artifact_storage_root() / name


def _image_candidates(payload: dict[str, Any]) -> list[Any]:
    candidates: list[Any] = []

    def add_items(value: Any) -> None:
        if isinstance(value, list):
            candidates.extend(value)

    result = payload.get("result")
    output = payload.get("output")
    add_items(payload.get("data"))
    add_items(payload.get("images"))
    if isinstance(result, dict):
        add_items(result.get("data"))
        add_items(result.get("images"))
        result_output = result.get("output")
        if isinstance(result_output, dict):
            add_items(result_output.get("data"))
    if isinstance(output, dict):
        add_items(output.get("data"))
    return candidates


def _extract_image_urls(payload: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for item in _image_candidates(payload):
        if not isinstance(item, dict):
            continue
        url = _http_url(item.get("url")) or _http_url(item.get("image_url")) or _http_url(item.get("download_url"))
        if url:
            urls.append(url)
    return urls


def _extract_inline_images(payload: dict[str, Any] | None) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        return []
    images: list[dict[str, str]] = []
    for item in _image_candidates(payload):
        if not isinstance(item, dict):
            continue
        mime_type = str(item.get("mime_type") or item.get("content_type") or "image/png").strip() or "image/png"
        value = item.get("b64_json") or item.get("base64") or item.get("image_base64")
        source = "b64_json"
        raw_url = item.get("url")
        if not value and isinstance(raw_url, str):
            match = _DATA_IMAGE_RE.match(raw_url.strip())
            if match:
                mime_type = match.group(1).lower()
                value = match.group(2)
                source = "data_url"
        if isinstance(value, str) and value.strip():
            images.append({"b64": value.strip(), "mime_type": mime_type.lower(), "source": source})
    return images


def _extract_video_url(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    result = payload.get("result") if isinstance(payload, dict) else None
    candidates: list[Any] = []
    if isinstance(data, dict):
        candidates.extend([data.get("output"), data])
    if isinstance(result, dict):
        result_data = result.get("data")
        if isinstance(result_data, dict):
            candidates.extend([result_data.get("output"), result_data])
        candidates.extend([result.get("output"), result])
    candidates.extend([payload.get("output"), payload])
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        url = _http_url(candidate.get("url")) or _http_url(candidate.get("video_url"))
        if url:
            return url
    return ""


def extract_media_urls(media_type: str, payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(payload, dict):
        return []
    if media_type == "image":
        return _extract_image_urls(payload)
    if media_type == "video":
        url = _extract_video_url(payload)
        return [url] if url else []
    return []


async def record_media_artifacts(
    db: AsyncSession,
    *,
    user_id: str,
    api_key_id: str | None = None,
    media_type: str,
    endpoint: str,
    model: str,
    provider_model: str = "",
    payload: dict[str, Any] | None = None,
    urls: Iterable[str] | None = None,
    status: str = "completed",
    source_type: str = "",
    source_id: str = "",
    upstream_request_id: str = "",
    route_reason: str = "",
    cost_cents: int = 0,
    completed_at: datetime | None = None,
) -> int:
    media_urls = list(urls or extract_media_urls(media_type, payload))
    media_urls = [url.strip() for url in media_urls if isinstance(url, str) and url.strip().startswith(_HTTP_PREFIXES)]
    inline_images = _extract_inline_images(payload) if media_type == "image" else []
    if not media_urls and not inline_images:
        return 0

    created = 0
    for index, url in enumerate(media_urls[:16]):
        db.add(
            MediaArtifact(
                id=generate_id("ma_"),
                user_id=user_id,
                api_key_id=api_key_id or None,
                media_type=media_type,
                endpoint=endpoint,
                model=model,
                provider_model=provider_model,
                status=status,
                url=url.strip(),
                thumbnail_url="",
                source_type=source_type,
                source_id=source_id,
                upstream_request_id=upstream_request_id,
                route_reason=route_reason,
                cost_cents=int(cost_cents or 0),
                metadata_json=json.dumps({"index": index}, separators=(",", ":")),
                completed_at=completed_at or datetime.utcnow(),
            )
        )
        created += 1
    for index, image in enumerate(inline_images[:16], start=created):
        artifact_id = generate_id("ma_")
        mime_type = image["mime_type"] if image["mime_type"] in _IMAGE_MIME_EXTENSIONS else "image/png"
        extension = _IMAGE_MIME_EXTENSIONS.get(mime_type, "png")
        storage_name = f"{artifact_id}.{extension}"
        storage_path = media_artifact_storage_path(storage_name)
        if storage_path is None:
            continue
        try:
            normalized_b64 = re.sub(r"\s+", "", image["b64"])
            content = base64.b64decode(normalized_b64, validate=True)
        except (binascii.Error, ValueError):
            logger.warning("skip invalid inline image artifact payload source_id=%s index=%s", source_id, index)
            continue
        await asyncio.to_thread(storage_path.write_bytes, content)
        db.add(
            MediaArtifact(
                id=artifact_id,
                user_id=user_id,
                api_key_id=api_key_id or None,
                media_type=media_type,
                endpoint=endpoint,
                model=model,
                provider_model=provider_model,
                status=status,
                url=f"/v1/media-artifacts/{artifact_id}/content",
                thumbnail_url="",
                source_type=source_type,
                source_id=source_id,
                upstream_request_id=upstream_request_id,
                route_reason=route_reason,
                cost_cents=int(cost_cents or 0),
                metadata_json=json.dumps(
                    {
                        "index": index,
                        "source": image.get("source") or "b64_json",
                        "storage_name": storage_name,
                        "content_type": mime_type,
                    },
                    separators=(",", ":"),
                ),
                completed_at=completed_at or datetime.utcnow(),
            )
        )
        created += 1
    return created


async def record_media_artifacts_best_effort(db: AsyncSession, **kwargs: Any) -> int:
    should_commit = bool(kwargs.pop("commit", False))
    try:
        created = await record_media_artifacts(db, **kwargs)
        if created and should_commit:
            await db.commit()
        return created
    except Exception:
        if should_commit:
            try:
                await db.rollback()
            except Exception:
                logger.exception("failed to rollback media artifact write")
        logger.exception("failed to record media artifacts")
        return 0
