#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx

from app import gemini_cpa


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"missing required env: {name}")
    return value


def _extract_text(data: dict[str, Any]) -> str:
    pieces: list[str] = []
    for choice in data.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            pieces.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    pieces.append(part["text"])
    return "".join(pieces).strip()


def _extract_responses_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    pieces: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"output_text", "text"} and isinstance(item.get("text"), str):
            pieces.append(item["text"])
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") in {"output_text", "text"} and isinstance(part.get("text"), str):
                pieces.append(part["text"])
    return "".join(pieces).strip()


async def _post(client: httpx.AsyncClient, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    response = await client.post(url, json=payload, headers=headers)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("upstream returned non-object JSON")
    return data


async def main() -> int:
    channel = gemini_cpa.GeminiCpaChannel(
        public_id="smoke",
        channel_id="smoke",
        provider_model=os.getenv("COINCOIN_GEMINI_CPA_CHAT_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash",
        upstream_url=_require_env("COINCOIN_GEMINI_CPA_BASE_URL"),
        api_key=_require_env("COINCOIN_GEMINI_CPA_API_KEY"),
        auth_style=os.getenv("COINCOIN_GEMINI_CPA_AUTH_STYLE", "bearer").strip() or "bearer",
    )
    image_model = os.getenv("COINCOIN_GEMINI_CPA_IMAGE_MODEL", "gemini-3.1-flash-image").strip() or "gemini-3.1-flash-image"

    headers = gemini_cpa.build_headers(channel)
    chat_url = gemini_cpa.chat_completions_url(channel)
    responses_url = gemini_cpa.responses_url(channel)
    timeout = httpx.Timeout(120.0, connect=20.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        chat_payload = {
            "model": channel.provider_model,
            "messages": [{"role": "user", "content": "Reply with only: OK"}],
            "max_tokens": 8,
        }
        chat_data = await _post(client, chat_url, chat_payload, headers)
        chat_text = _extract_text(chat_data)
        if not chat_text:
            raise RuntimeError(f"chat response had no text: {json.dumps(chat_data)[:300]}")
        print(f"chat_ok model={channel.provider_model} text={chat_text[:40]!r}")

        responses_payload = {
            "model": channel.provider_model,
            "input": "Reply with only: OK",
            "max_output_tokens": 64,
        }
        responses_data = await _post(client, responses_url, responses_payload, headers)
        responses_text = _extract_responses_text(responses_data)
        if not responses_text:
            raise RuntimeError(f"responses response had no text: {json.dumps(responses_data)[:300]}")
        print(f"responses_ok model={channel.provider_model} text={responses_text[:40]!r}")

        image_payload = gemini_cpa.build_image_generation_payload(
            {"prompt": "A tiny blue coin icon on a plain white background", "size": "1024x1024"},
            image_model,
        )
        image_data = await _post(client, chat_url, image_payload, headers)
        translated = gemini_cpa.translate_image_response(image_data)
        items = translated.get("data") if isinstance(translated, dict) else None
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise RuntimeError(f"image response had no translated image: {json.dumps(image_data)[:300]}")
        b64_len = len(str(items[0].get("b64_json") or ""))
        if b64_len < 100:
            raise RuntimeError(f"translated image payload is too small: {b64_len}")
        print(f"image_ok model={image_model} b64_len={b64_len}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500]
        print(f"upstream_http_error status={exc.response.status_code} body={body}", file=sys.stderr)
        raise SystemExit(1)
