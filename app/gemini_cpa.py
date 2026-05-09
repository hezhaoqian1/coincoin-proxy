from __future__ import annotations

import base64
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import urlsplit, urlunsplit

from .config import settings


logger = logging.getLogger("coincoin.gemini_cpa")

DELIVERY_LANE = "cpa_gemini"
_DATA_URL_IMAGE_RE = re.compile(r"data:image/[^;]+;base64,([A-Za-z0-9+/=\r\n]+)")
_CHANNEL_STATE: Dict[str, Dict[str, float | int]] = {}


@dataclass(frozen=True)
class GeminiCpaChannel:
    public_id: str
    channel_id: str
    provider_model: str
    upstream_url: str
    api_key: str
    auth_style: str = "bearer"
    priority: int = 0
    weight: int = 1
    allowed_fails: int = 3
    cooldown_seconds: float = 30.0


class GeminiCpaChannelUnavailable(RuntimeError):
    pass


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_openai_base_url(base_url: str) -> str:
    cleaned = str(base_url or "").strip()
    while cleaned.endswith("}"):
        cleaned = cleaned[:-1]
    cleaned = cleaned.rstrip("/")
    if not cleaned:
        return ""

    parsed = urlsplit(cleaned)
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    elif not path.endswith("/v1"):
        path = f"{path}/v1"

    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def chat_completions_url(channel: GeminiCpaChannel) -> str:
    return f"{normalize_openai_base_url(channel.upstream_url)}/chat/completions"


def responses_url(channel: GeminiCpaChannel) -> str:
    return f"{normalize_openai_base_url(channel.upstream_url)}/responses"


def build_headers(channel: GeminiCpaChannel) -> Dict[str, str]:
    headers = {"content-type": "application/json"}
    if channel.auth_style == "azure":
        headers["api-key"] = channel.api_key
    else:
        headers["authorization"] = f"Bearer {channel.api_key}"
    return headers


def _default_allowed_fails(metadata: Dict[str, Any]) -> int:
    return max(
        1,
        _as_int(
            metadata.get("allowed_fails"),
            int(getattr(settings, "gemini_cpa_default_allowed_fails", 3) or 3),
        ),
    )


def _default_cooldown_seconds(metadata: Dict[str, Any]) -> float:
    return max(
        0.0,
        _as_float(
            metadata.get("cooldown_seconds"),
            float(getattr(settings, "gemini_cpa_default_cooldown_seconds", 30.0) or 30.0),
        ),
    )


def _channel_from_item(public_model: Any, backend: Any, item: Dict[str, Any]) -> GeminiCpaChannel:
    metadata = public_model.metadata if isinstance(getattr(public_model, "metadata", None), dict) else {}
    provider_model = str(
        item.get("upstream_model")
        or item.get("provider_model")
        or item.get("model")
        or getattr(backend, "model_id", "")
    ).strip()
    upstream_url = str(item.get("upstream_url") or getattr(backend, "upstream_url", "")).strip()
    api_key = str(item.get("api_key") or getattr(backend, "api_key", "")).strip()
    auth_style = str(item.get("auth_style") or getattr(backend, "auth_style", "bearer") or "bearer").strip()
    channel_id = str(
        item.get("channel_id")
        or item.get("id")
        or f"{getattr(public_model, 'public_id', '')}:{upstream_url}:{provider_model}"
    ).strip()
    return GeminiCpaChannel(
        public_id=str(getattr(public_model, "public_id", "") or ""),
        channel_id=channel_id,
        provider_model=provider_model,
        upstream_url=upstream_url,
        api_key=api_key,
        auth_style=auth_style,
        priority=_as_int(item.get("priority"), _as_int(metadata.get("priority"), 0)),
        weight=max(1, _as_int(item.get("weight"), _as_int(metadata.get("weight"), 1))),
        allowed_fails=max(1, _as_int(item.get("allowed_fails"), _default_allowed_fails(metadata))),
        cooldown_seconds=max(
            0.0,
            _as_float(item.get("cooldown_seconds"), _default_cooldown_seconds(metadata)),
        ),
    )


def channels_for_model(public_model: Any, backend: Any) -> List[GeminiCpaChannel]:
    metadata = public_model.metadata if isinstance(getattr(public_model, "metadata", None), dict) else {}
    raw_channels = metadata.get("cpa_gemini_channels")
    channels: List[GeminiCpaChannel] = []
    if isinstance(raw_channels, list):
        for item in raw_channels:
            if isinstance(item, dict):
                channel = _channel_from_item(public_model, backend, item)
                if channel.provider_model and channel.upstream_url and channel.api_key:
                    channels.append(channel)

    if channels:
        return channels

    return [
        _channel_from_item(
            public_model,
            backend,
            {
                "channel_id": metadata.get("channel_id"),
                "priority": metadata.get("priority"),
                "weight": metadata.get("weight"),
                "allowed_fails": metadata.get("allowed_fails"),
                "cooldown_seconds": metadata.get("cooldown_seconds"),
            },
        )
    ]


def _cooldown_until(channel_id: str) -> float:
    state = _CHANNEL_STATE.get(channel_id) or {}
    return _as_float(state.get("cooldown_until"), 0.0)


def _is_available(channel: GeminiCpaChannel, now: float | None = None) -> bool:
    return _cooldown_until(channel.channel_id) <= (time.time() if now is None else now)


def _pick_weighted(channels: List[GeminiCpaChannel]) -> GeminiCpaChannel:
    total = sum(max(1, channel.weight) for channel in channels)
    cursor = random.uniform(0, total)
    upto = 0.0
    for channel in channels:
        upto += max(1, channel.weight)
        if cursor <= upto:
            return channel
    return channels[-1]


def select_channel(public_model: Any, backend: Any) -> GeminiCpaChannel:
    configured = channels_for_model(public_model, backend)
    available = [channel for channel in configured if _is_available(channel)]
    if not available:
        soonest = min(_cooldown_until(channel.channel_id) for channel in configured)
        retry_after = max(1, int(soonest - time.time()))
        raise GeminiCpaChannelUnavailable(
            f"All Gemini CPA channels for {getattr(public_model, 'public_id', '')} are cooling down. "
            f"Retry after {retry_after}s."
        )

    best_priority = min(channel.priority for channel in available)
    candidates = [channel for channel in available if channel.priority == best_priority]
    return _pick_weighted(candidates)


def record_success(channel: GeminiCpaChannel) -> None:
    _CHANNEL_STATE.pop(channel.channel_id, None)


def record_failure(channel: GeminiCpaChannel) -> None:
    state = _CHANNEL_STATE.setdefault(channel.channel_id, {"failures": 0, "cooldown_until": 0})
    failures = int(state.get("failures") or 0) + 1
    state["failures"] = failures
    if failures >= channel.allowed_fails:
        state["cooldown_until"] = time.time() + channel.cooldown_seconds
        state["failures"] = 0
        logger.warning(
            "gemini_cpa_channel_cooldown channel=%s public_id=%s provider_model=%s cooldown_seconds=%s",
            channel.channel_id,
            channel.public_id,
            channel.provider_model,
            channel.cooldown_seconds,
        )


def should_record_failure(status_code: int) -> bool:
    return status_code in {408, 409, 429} or status_code >= 500


def _map_image_size_to_aspect_ratio(size: str) -> str:
    return {
        "1024x1024": "1:1",
        "1792x1024": "16:9",
        "1024x1792": "9:16",
        "1280x896": "4:3",
        "896x1280": "3:4",
    }.get(size, "1:1")


def _image_config_from_size(size: str) -> Dict[str, Any]:
    if not size:
        return {}
    return {"aspect_ratio": _map_image_size_to_aspect_ratio(size)}


def build_image_generation_payload(payload: Dict[str, Any], provider_model: str) -> Dict[str, Any]:
    prompt = str(payload.get("prompt") or "").strip() or " "
    size = str(payload.get("size") or "").strip()
    request_payload: Dict[str, Any] = {
        "model": provider_model,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
    }
    image_config = _image_config_from_size(size)
    if image_config:
        request_payload["image_config"] = image_config
    return request_payload


def build_image_edit_payload(
    form_fields: List[Tuple[str, str]],
    file_fields: List[Tuple[str, Tuple[str, bytes, str]]],
    provider_model: str,
) -> Dict[str, Any]:
    prompt = ""
    size = ""
    content_parts: List[Dict[str, Any]] = []

    for key, value in form_fields:
        if key == "prompt":
            prompt = value
        elif key == "size":
            size = value

    for key, (_, content, content_type) in file_fields:
        if key not in {"image", "image[]"}:
            continue
        mime_type = content_type or "application/octet-stream"
        encoded = base64.b64encode(content).decode("utf-8")
        content_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
            }
        )

    if not content_parts:
        raise ValueError("Gemini image edit requires at least one image.")

    content_parts.append({"type": "text", "text": prompt or " "})
    request_payload: Dict[str, Any] = {
        "model": provider_model,
        "messages": [{"role": "user", "content": content_parts}],
        "modalities": ["image", "text"],
    }
    image_config = _image_config_from_size(size)
    if image_config:
        request_payload["image_config"] = image_config
    return request_payload


def _b64_from_data_url(value: str) -> str:
    raw = (value or "").strip()
    if raw.startswith("data:") and ";base64," in raw:
        return raw.split(";base64,", 1)[1].strip()
    return raw


def _append_image_url(output_images: List[Dict[str, str]], value: Any) -> None:
    if isinstance(value, dict):
        value = value.get("url")
    image_b64 = _b64_from_data_url(str(value or ""))
    if image_b64:
        output_images.append({"b64_json": image_b64})


def _extract_images_from_content(output_images: List[Dict[str, str]], content: Any) -> None:
    if isinstance(content, str):
        for match in _DATA_URL_IMAGE_RE.finditer(content):
            output_images.append({"b64_json": match.group(1).strip()})
        return

    if not isinstance(content, list):
        return

    for part in content:
        if not isinstance(part, dict):
            continue
        image_url = part.get("image_url")
        if image_url:
            _append_image_url(output_images, image_url)
        if part.get("type") in {"image_url", "output_image"} and part.get("url"):
            _append_image_url(output_images, part.get("url"))


def translate_image_response(data: Dict[str, Any]) -> Dict[str, Any]:
    output_images: List[Dict[str, str]] = []
    for choice in data.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            continue
        for image in message.get("images") or []:
            if not isinstance(image, dict):
                continue
            _append_image_url(output_images, image.get("image_url"))
        _extract_images_from_content(output_images, message.get("content"))

    return {
        "created": int(data.get("created") or time.time()) if isinstance(data, dict) else int(time.time()),
        "data": output_images,
    }


def iter_channel_debug_headers(channel: GeminiCpaChannel) -> Iterable[Tuple[str, str]]:
    yield ("x-coincoin-gemini-channel", channel.channel_id)
    yield ("x-coincoin-gemini-provider-model", channel.provider_model)
