from __future__ import annotations

import json
import secrets
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlsplit

from .usage_buffer import extract_cache_read_tokens, extract_total_input_tokens

ANTHROPIC_COMPATIBLE_CHANNEL_TYPE = "anthropic_compatible"
ANTHROPIC_MESSAGES_TRANSFORM_PROFILE = "anthropic_messages"
ANTHROPIC_X_API_KEY_AUTH_STYLES = frozenset({"x-api-key", "anthropic_x_api_key", "anthropic"})
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
CLAUDE_CODE_DEFAULT_BETA = (
    "claude-code-20250219,"
    "interleaved-thinking-2025-05-14,"
    "thinking-token-count-2026-05-13,"
    "context-management-2025-06-27,"
    "prompt-caching-scope-2026-01-05,"
    "effort-2025-11-24"
)
CLAUDE_CODE_DEFAULT_HEADERS = {
    "anthropic-beta": CLAUDE_CODE_DEFAULT_BETA,
    "anthropic-dangerous-direct-browser-access": "true",
    "user-agent": "claude-cli/2.1.198 (external, sdk-cli)",
    "x-app": "cli",
    "x-claude-code-session-id": "coincoin-proxy",
    "x-stainless-arch": "arm64",
    "x-stainless-lang": "js",
    "x-stainless-os": "MacOS",
    "x-stainless-package-version": "0.94.0",
    "x-stainless-runtime": "node",
    "x-stainless-runtime-version": "v26.3.0",
}


def is_anthropic_compatible_config(cfg: Any) -> bool:
    channel_type = str(getattr(cfg, "channel_type", "") or "").strip().lower()
    transform_profile = str(getattr(cfg, "transform_profile", "") or "").strip().lower()
    return channel_type == ANTHROPIC_COMPATIBLE_CHANNEL_TYPE or transform_profile == ANTHROPIC_MESSAGES_TRANSFORM_PROFILE


def is_claude_code_upstream_config(cfg: Any) -> bool:
    if not is_anthropic_compatible_config(cfg):
        return False
    cost_tier = str(getattr(cfg, "cost_tier", "") or "").strip().lower()
    fingerprint = str(getattr(cfg, "provider_account_fingerprint", "") or "").strip().lower()
    return cost_tier == "claude-code" or "claude-code" in fingerprint


def ensure_claude_code_upstream_headers(headers: Dict[str, str], cfg: Any) -> Dict[str, str]:
    if not is_claude_code_upstream_config(cfg):
        return headers
    existing_beta = str(headers.get("anthropic-beta") or "")
    if existing_beta:
        beta_parts = [item.strip() for item in existing_beta.split(",") if item.strip()]
        for item in CLAUDE_CODE_DEFAULT_BETA.split(","):
            if item not in beta_parts:
                beta_parts.append(item)
        headers["anthropic-beta"] = ",".join(beta_parts)
    else:
        headers["anthropic-beta"] = CLAUDE_CODE_DEFAULT_BETA

    for name, value in CLAUDE_CODE_DEFAULT_HEADERS.items():
        if name == "anthropic-beta":
            continue
        if name in {"anthropic-dangerous-direct-browser-access", "user-agent", "x-app"}:
            headers[name] = value
        else:
            headers.setdefault(name, value)
    return headers


def ensure_claude_code_messages_url(upstream_url: str, cfg: Any) -> str:
    if not is_claude_code_upstream_config(cfg):
        return upstream_url
    query_params = parse_qsl(urlsplit(str(upstream_url or "")).query, keep_blank_values=True)
    if any(key == "beta" for key, _value in query_params):
        return upstream_url
    separator = "&" if "?" in upstream_url else "?"
    return f"{upstream_url}{separator}beta=true"


def build_anthropic_messages_url(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return "/v1/messages"
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


def openai_chat_to_anthropic_messages_payload(payload: Dict[str, Any], *, model: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "model": model,
        "messages": _openai_messages_to_anthropic_messages(payload.get("messages") or []),
        "max_tokens": _openai_max_tokens(payload),
        "stream": bool(payload.get("stream")),
    }
    system = _extract_openai_system(payload.get("messages") or [])
    if system:
        result["system"] = system
    tools = _openai_tools_to_anthropic_tools(payload.get("tools"))
    if tools:
        result["tools"] = tools
    tool_choice = _openai_tool_choice_to_anthropic(payload.get("tool_choice"))
    if tool_choice:
        result["tool_choice"] = tool_choice
    if "temperature" in payload:
        result["temperature"] = payload["temperature"]
    if "top_p" in payload:
        result["top_p"] = payload["top_p"]
    if "top_k" in payload:
        result["top_k"] = payload["top_k"]
    if "stop" in payload:
        stop = payload.get("stop")
        result["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    return result


def anthropic_message_to_openai_chat_response(data: Dict[str, Any], *, display_model: str) -> Dict[str, Any]:
    content_text: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    reasoning_text: List[str] = []

    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type == "text":
            content_text.append(str(block.get("text") or ""))
        elif block_type == "tool_use":
            tool_input = block.get("input")
            if isinstance(tool_input, str):
                arguments = tool_input
            else:
                arguments = json.dumps(tool_input or {}, ensure_ascii=False, separators=(",", ":"))
            tool_calls.append(
                {
                    "id": str(block.get("id") or f"toolu_{len(tool_calls)}"),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": arguments,
                    },
                }
            )
        elif block_type == "thinking":
            reasoning_text.append(str(block.get("thinking") or ""))

    message: Dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_text) if content_text else None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning_text:
        message["reasoning_content"] = "".join(reasoning_text)

    usage = _anthropic_usage_to_openai_usage(data.get("usage") or {})
    return {
        "id": str(data.get("id") or f"chatcmpl-{secrets.token_hex(12)}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": display_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": anthropic_stop_reason_to_openai(data.get("stop_reason"), bool(tool_calls)),
            }
        ],
        "usage": usage,
    }


def anthropic_stop_reason_to_openai(reason: Any, saw_tool_call: bool = False) -> str:
    value = str(reason or "").strip()
    if saw_tool_call or value == "tool_use":
        return "tool_calls"
    if value == "max_tokens":
        return "length"
    if value == "stop_sequence":
        return "stop"
    if value == "refusal":
        return "content_filter"
    return "stop"


def _openai_max_tokens(payload: Dict[str, Any]) -> int:
    for key in ("max_tokens", "max_completion_tokens"):
        value = payload.get(key)
        if value is not None:
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                break
    return 4096


def _extract_openai_system(messages: Any) -> str:
    parts: List[str] = []
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        text = _openai_content_to_text(message.get("content"))
        if text:
            parts.append(text)
    return "\n".join(parts)


def _openai_messages_to_anthropic_messages(messages: Any) -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    if not isinstance(messages, list):
        return converted
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        if role == "system":
            continue
        if role == "tool":
            tool_result = {
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id") or ""),
                "content": _openai_tool_result_content(message.get("content")),
            }
            converted.append({"role": "user", "content": [tool_result]})
            continue
        if role not in {"user", "assistant"}:
            role = "user"

        content_blocks = _openai_content_to_anthropic_blocks(message.get("content"), role=role)
        if role == "assistant":
            for tool_call in message.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                arguments = function.get("arguments")
                tool_input: Any = {}
                if isinstance(arguments, str) and arguments.strip():
                    try:
                        tool_input = json.loads(arguments)
                    except json.JSONDecodeError:
                        tool_input = arguments
                elif isinstance(arguments, dict):
                    tool_input = arguments
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(tool_call.get("id") or f"call_{len(content_blocks)}"),
                        "name": str(function.get("name") or tool_call.get("name") or ""),
                        "input": tool_input,
                    }
                )
        if len(content_blocks) == 1 and content_blocks[0].get("type") == "text":
            content: Any = str(content_blocks[0].get("text") or "")
        else:
            content = content_blocks
        converted.append({"role": role, "content": content})
    return converted


def _openai_content_to_anthropic_blocks(content: Any, *, role: str) -> List[Dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]

    blocks: List[Dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            if part is not None:
                blocks.append({"type": "text", "text": str(part)})
            continue
        part_type = str(part.get("type") or "")
        if part_type in {"text", "input_text", "output_text"}:
            blocks.append({"type": "text", "text": str(part.get("text") or "")})
        elif role == "user" and part_type == "image_url":
            source = _openai_image_url_to_anthropic_source(part.get("image_url"))
            if source:
                blocks.append({"type": "image", "source": source})
        elif role == "user" and part_type == "input_image":
            source = _openai_image_url_to_anthropic_source(part.get("image_url") or part.get("url"))
            if source:
                blocks.append({"type": "image", "source": source})
        elif role == "assistant" and part_type == "tool_use":
            blocks.append(part)
        elif role == "user" and part_type == "tool_result":
            blocks.append(part)
    return blocks


def _openai_image_url_to_anthropic_source(image_url: Any) -> Optional[Dict[str, Any]]:
    url = ""
    if isinstance(image_url, dict):
        url = str(image_url.get("url") or "")
    elif isinstance(image_url, str):
        url = image_url
    if not url:
        return None
    if url.startswith("data:") and ";base64," in url:
        media_type, data = url[5:].split(";base64,", 1)
        return {"type": "base64", "media_type": media_type or "image/png", "data": data}
    return {"type": "url", "url": url}


def _openai_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}:
                text_parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                text_parts.append(part)
        return "".join(text_parts)
    return str(content)


def _openai_tool_result_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    blocks = _openai_content_to_anthropic_blocks(content, role="user")
    if blocks:
        return blocks
    if content is None:
        return ""
    return str(content)


def _openai_tools_to_anthropic_tools(tools: Any) -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    if not isinstance(tools, list):
        return converted
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        if tool.get("type") == "function" and function:
            converted.append(
                {
                    "name": str(function.get("name") or ""),
                    "description": str(function.get("description") or ""),
                    "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
                }
            )
        elif tool.get("name"):
            converted.append(
                {
                    "name": str(tool.get("name") or ""),
                    "description": str(tool.get("description") or ""),
                    "input_schema": tool.get("input_schema") or tool.get("parameters") or {"type": "object", "properties": {}},
                }
            )
    return converted


def _openai_tool_choice_to_anthropic(tool_choice: Any) -> Optional[Dict[str, Any]]:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "required":
            return {"type": "any"}
        if tool_choice in {"auto", "any", "none"}:
            return {"type": tool_choice}
        return None
    if isinstance(tool_choice, dict):
        choice_type = str(tool_choice.get("type") or "")
        if choice_type == "function":
            function = tool_choice.get("function") if isinstance(tool_choice.get("function"), dict) else {}
            name = str(function.get("name") or "")
            if name:
                return {"type": "tool", "name": name}
        if choice_type in {"auto", "any", "none"}:
            return {"type": choice_type}
    return None


def _anthropic_usage_to_openai_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    input_tokens = extract_total_input_tokens(usage)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    body: Dict[str, Any] = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    cache_read = extract_cache_read_tokens(usage)
    if cache_read:
        body["prompt_tokens_details"] = {"cached_tokens": cache_read}
    return body


def openai_usage_from_anthropic_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    return _anthropic_usage_to_openai_usage(usage)
