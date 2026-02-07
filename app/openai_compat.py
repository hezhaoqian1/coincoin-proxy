import json
import secrets
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .proxy import authorize_request, filter_headers, get_http_client, get_stream_client, proxy_responses, responses_health
from .usage_buffer import usage_buffer


router = APIRouter(prefix="/v1", tags=["openai-compat"])


# ============== 标准 OpenAI 错误格式 ==============
def openai_error(message: str, error_type: str = "invalid_request_error", 
                 param: Optional[str] = None, code: Optional[str] = None, 
                 status_code: int = 400) -> JSONResponse:
    """返回标准 OpenAI 错误格式"""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": param,
                "code": code,
            }
        }
    )


@router.get("/models")
async def list_models():
    """列出可用模型"""
    return {
        "object": "list",
        "data": [
            {
                "id": settings.fixed_model,
                "object": "model",
                "created": 1700000000,
                "owned_by": "azure-openai",
            }
        ],
    }


@router.get("/models/{model_id}")
async def get_model(model_id: str):
    """获取单个模型信息"""
    # 所有模型请求都返回固定模型
    return {
        "id": settings.fixed_model,
        "object": "model",
        "created": 1700000000,
        "owned_by": "azure-openai",
    }


@router.get("/responses")
async def responses_alias_health():
    return await responses_health()


@router.post("/responses")
async def responses_alias(request: Request, db: AsyncSession = Depends(get_db)):
    return await proxy_responses(request, db)


@router.post("/chat/completions")
async def chat_completions(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        user = await authorize_request(request, db)
    except HTTPException as e:
        # 转换为 OpenAI 标准错误格式
        if e.status_code == 401:
            return openai_error("Invalid API key provided", "authentication_error", code="invalid_api_key", status_code=401)
        elif e.status_code == 403:
            return openai_error("Access denied", "permission_error", code="access_denied", status_code=403)
        elif e.status_code == 429:
            return openai_error(e.detail, "rate_limit_error", code="rate_limit_exceeded", status_code=429)
        raise

    try:
        payload = await request.json()
    except Exception:
        return openai_error("Invalid JSON payload", "invalid_request_error", code="invalid_json")

    if not isinstance(payload, dict):
        return openai_error("Request body must be a JSON object", "invalid_request_error")

    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        return openai_error("'messages' must be an array", "invalid_request_error", param="messages")

    # ============== 处理 messages 格式兼容性 ==============
    # Azure Responses API 使用与 OpenAI Chat Completions 完全不同的消息格式
    # 
    # OpenAI Chat Completions 格式:
    #   {"role": "assistant", "content": null, "tool_calls": [...]}
    #   {"role": "tool", "tool_call_id": "xxx", "content": "result"}
    #
    # Azure Responses API 格式:
    #   {"type": "function_call", "call_id": "xxx", "name": "fn", "arguments": "{}"}
    #   {"type": "function_call_output", "call_id": "xxx", "output": "result"}
    #
    def convert_messages_for_responses_api(msgs: list) -> list:
        """将 OpenAI Chat Completions 消息格式转换为 Azure Responses API 格式"""
        converted = []
        for msg in msgs:
            if not isinstance(msg, dict):
                converted.append(msg)
                continue
            
            role = msg.get("role")
            
            # 1. 处理带 tool_calls 的 assistant 消息
            if role == "assistant" and "tool_calls" in msg:
                tool_calls = msg.get("tool_calls", [])
                content = msg.get("content")
                
                # 如果有文本内容，先添加 assistant 消息
                if content:
                    converted.append({"role": "assistant", "content": content})
                
                # 将每个 tool_call 转换为 function_call item
                # 兼容两种格式：
                # - 标准 OpenAI: {"id": "xxx", "type": "function", "function": {"name": "...", "arguments": "..."}}
                # - 简化格式 (nanobot): {"name": "read_file", "arguments_size": 83} 或 {"name": "...", "arguments": "..."}
                for idx, tc in enumerate(tool_calls):
                    # 标准格式：有 function 字段
                    if "function" in tc:
                        func = tc.get("function", {})
                        call_id = tc.get("id", f"call_{idx}")
                        name = func.get("name", "")
                        arguments = func.get("arguments", "{}")
                    else:
                        # 简化格式：name 直接在顶层
                        call_id = tc.get("id", f"call_{idx}")
                        name = tc.get("name", "")
                        arguments = tc.get("arguments", "{}")
                    
                    converted.append({
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
                    })
                continue
            
            # 2. 处理 tool 角色消息 (工具结果)
            if role == "tool":
                call_id = msg.get("tool_call_id", "")
                output = msg.get("content", "")
                converted.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output if output is not None else "",
                })
                continue
            
            # 3. 普通消息：处理 content: null
            result = dict(msg)
            if result.get("content") is None and "content" in result:
                result["content"] = ""
            converted.append(result)
        
        return converted

    converted_messages = convert_messages_for_responses_api(messages)

    # ============== 构建 Responses API payload ==============
    resp_payload: Dict[str, Any] = {
        "model": settings.fixed_model,
        "input": converted_messages,
        "stream": bool(payload.get("stream")),
    }
    
    # max_tokens -> max_output_tokens
    if "max_tokens" in payload:
        resp_payload["max_output_tokens"] = payload.get("max_tokens")
    if "max_completion_tokens" in payload:
        resp_payload["max_output_tokens"] = payload.get("max_completion_tokens")
    
    # 注意: gpt-5.2-codex 模型不支持 temperature, top_p, presence_penalty, frequency_penalty 等参数
    # 只透传 stop 和 seed（如果模型支持）
    if "stop" in payload:
        resp_payload["stop"] = payload["stop"]
    
    # seed 参数（某些模型支持）
    # if "seed" in payload:
    #     resp_payload["seed"] = payload["seed"]
    
    # response_format 支持 (JSON mode / Structured Outputs)
    if "response_format" in payload:
        rf = payload["response_format"]
        if isinstance(rf, dict):
            rf_type = rf.get("type")
            if rf_type == "json_object":
                # JSON mode
                resp_payload["text"] = {"format": {"type": "json_object"}}
            elif rf_type == "json_schema":
                # Structured Outputs
                resp_payload["text"] = {
                    "format": {
                        "type": "json_schema",
                        "json_schema": rf.get("json_schema", {}),
                    }
                }
            # text 类型是默认值，不需要特别处理
    
    # Tools/Functions 支持 - 需要转换格式
    # 兼容三种输入格式：
    # 1. 标准 OpenAI:  {"type":"function","function":{"name":"x","parameters":{...}}}
    # 2. Responses API: {"type":"function","name":"x","parameters":{...}}
    # 3. 简化格式 (nanobot): {"name":"read_file","params":["path"]}
    if "tools" in payload:
        converted_tools = []
        for tool in payload["tools"]:
            # 格式 1: 标准 OpenAI Chat Completions
            if tool.get("type") == "function" and "function" in tool:
                func = tool["function"]
                converted_tool = {
                    "type": "function",
                    "name": func.get("name"),
                    "description": func.get("description", ""),
                    "parameters": func.get("parameters", {}),
                }
                if "strict" in func:
                    converted_tool["strict"] = func["strict"]
                converted_tools.append(converted_tool)
            # 格式 2: 已经是 Responses API 格式
            elif tool.get("type") == "function" and "name" in tool:
                converted_tools.append(tool)
            # 格式 3: 简化格式 (nanobot) - {"name": "x", "params": [...]}
            elif "name" in tool and ("params" in tool or "parameters" not in tool):
                params = tool.get("params", [])
                # 将 params 数组转换为 JSON Schema 格式
                properties = {}
                if isinstance(params, list):
                    for p in params:
                        if isinstance(p, str):
                            properties[p] = {"type": "string"}
                        elif isinstance(p, dict):
                            properties.update(p)
                converted_tool = {
                    "type": "function",
                    "name": tool.get("name"),
                    "description": tool.get("description", ""),
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                    },
                }
                converted_tools.append(converted_tool)
            else:
                # 未知格式，尝试直接使用
                converted_tools.append(tool)
        resp_payload["tools"] = converted_tools
    
    for field in ("tool_choice", "parallel_tool_calls"):
        if field in payload:
            resp_payload[field] = payload[field]

    upstream_url = f"{settings.upstream_base_url.rstrip('/')}/responses"
    headers = {
        "api-key": settings.upstream_api_key,
        "content-type": "application/json",
    }

    def build_chat_response(resp: Dict) -> Dict:
        usage = resp.get("usage") or {}
        prompt_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
        completion_tokens = usage.get("output_tokens") or usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens") or (
            (prompt_tokens or 0) + (completion_tokens or 0)
        )

        text_chunks = []
        tool_calls = []
        finish_reason = "stop"

        for item in resp.get("output", []) or []:
            item_type = item.get("type")
            
            # 处理文本消息
            if item_type == "message":
                for c in item.get("content", []) or []:
                    if c.get("type") in ("output_text", "text") and c.get("text"):
                        text_chunks.append(c.get("text"))
            elif item_type in ("output_text", "text") and item.get("text"):
                text_chunks.append(item.get("text"))
            
            # 处理 function_call / tool_use (Responses API 格式)
            elif item_type in ("function_call", "tool_use", "function"):
                tool_call = {
                    "id": item.get("id") or item.get("call_id") or f"call_{secrets.token_hex(12)}",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or item.get("function", {}).get("name", ""),
                        "arguments": item.get("arguments") or item.get("function", {}).get("arguments", "{}"),
                    }
                }
                # arguments 可能是 dict，需要转成 string
                if isinstance(tool_call["function"]["arguments"], dict):
                    tool_call["function"]["arguments"] = json.dumps(tool_call["function"]["arguments"], ensure_ascii=False)
                tool_calls.append(tool_call)
                finish_reason = "tool_calls"

        if not text_chunks and resp.get("output_text"):
            text_chunks.append(resp.get("output_text"))
        content = "".join(text_chunks) if text_chunks else None

        # 构建 message
        message: Dict[str, object] = {"role": "assistant"}
        if content:
            message["content"] = content
        else:
            message["content"] = None
        if tool_calls:
            message["tool_calls"] = tool_calls

        return {
            "id": resp.get("id") or f"chatcmpl-{secrets.token_hex(12)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": settings.fixed_model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "total_tokens": int(total_tokens or 0),
            },
        }

    if resp_payload.get("stream"):
        stream_client = await get_stream_client()
        request_obj = stream_client.build_request("POST", upstream_url, json=resp_payload, headers=headers)
        upstream = await stream_client.send(request_obj, stream=True)
        content_type = upstream.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            try:
                body = await upstream.aread()
            finally:
                await upstream.aclose()
            response_headers = filter_headers(dict(upstream.headers))
            response_headers.pop("content-length", None)
            if "application/json" in content_type:
                data = json.loads(body.decode("utf-8"))
                return JSONResponse(content=build_chat_response(data), status_code=upstream.status_code, headers=response_headers)
            return Response(content=body, status_code=upstream.status_code, headers=response_headers, media_type=content_type)

        stream_id = f"chatcmpl-{secrets.token_hex(12)}"
        tool_call_index = 0
        has_tool_calls = False
        first_content_sent = False

        async def iter_events():
            nonlocal tool_call_index, has_tool_calls, first_content_sent
            try:
                async for line in upstream.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except Exception:
                        continue
                    event_type = event.get("type")
                    
                    # 处理文本内容
                    if event_type in ("response.output_text.delta", "response.output_text.chunk"):
                        delta = event.get("delta")
                        if isinstance(delta, dict):
                            delta_text = delta.get("text")
                        else:
                            delta_text = delta if isinstance(delta, str) else None
                        if delta_text:
                            # 首个内容 chunk 需要带 role (OpenAI 标准)
                            if not first_content_sent:
                                first_content_sent = True
                                delta_obj = {"role": "assistant", "content": delta_text}
                            else:
                                delta_obj = {"content": delta_text}
                            chunk = {
                                "id": stream_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": settings.fixed_model,
                                "choices": [{"index": 0, "delta": delta_obj, "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    
                    # 处理 function_call 开始
                    elif event_type in ("response.function_call_arguments.start", "response.output_item.added"):
                        item = event.get("item", {})
                        if item.get("type") in ("function_call", "tool_use", "function"):
                            has_tool_calls = True
                            func_name = item.get("name") or item.get("function", {}).get("name", "")
                            call_id = item.get("id") or item.get("call_id") or f"call_{secrets.token_hex(12)}"
                            # 首个 chunk 需要带 role
                            delta_obj: Dict[str, Any] = {
                                "tool_calls": [{
                                    "index": tool_call_index,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": func_name, "arguments": ""}
                                }]
                            }
                            if not first_content_sent:
                                first_content_sent = True
                                delta_obj["role"] = "assistant"
                            chunk = {
                                "id": stream_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": settings.fixed_model,
                                "choices": [{"index": 0, "delta": delta_obj, "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    
                    # 处理 function_call 参数增量
                    elif event_type == "response.function_call_arguments.delta":
                        delta_args = event.get("delta", "")
                        if delta_args:
                            chunk = {
                                "id": stream_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": settings.fixed_model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [{
                                            "index": tool_call_index,
                                            "function": {"arguments": delta_args}
                                        }]
                                    },
                                    "finish_reason": None
                                }],
                            }
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    
                    # 处理 function_call 完成
                    elif event_type == "response.function_call_arguments.done":
                        tool_call_index += 1
                    
                    # 处理上游错误事件
                    elif event_type in ("response.failed", "response.error", "error"):
                        error_info = event.get("error", {})
                        error_msg = error_info.get("message") if isinstance(error_info, dict) else str(error_info)
                        error_code = error_info.get("code") if isinstance(error_info, dict) else None
                        if not error_msg:
                            error_msg = event.get("message", "Unknown upstream error")
                        
                        # 发送 SSE error 事件（OpenAI 兼容格式）
                        error_data = {
                            "error": {
                                "message": error_msg,
                                "type": "server_error",
                                "code": error_code,
                            }
                        }
                        yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    
                    # 处理响应完成
                    if event_type in ("response.output_text.done", "response.completed"):
                        finish_reason = "tool_calls" if has_tool_calls else "stop"
                        finish = {
                            "id": stream_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": settings.fixed_model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                        }
                        yield f"data: {json.dumps(finish)}\n\n"
                        break
                yield "data: [DONE]\n\n"
            except Exception:
                # 流处理异常，静默结束
                pass
            finally:
                await upstream.aclose()

        if upstream.status_code < 400:
            await usage_buffer.add(user.id, tokens=0, requests=1)
        stream_headers = filter_headers(dict(upstream.headers))
        stream_headers.pop("content-length", None)
        stream_headers.setdefault("cache-control", "no-cache")
        stream_headers.setdefault("x-accel-buffering", "no")
        return StreamingResponse(iter_events(), status_code=upstream.status_code, headers=stream_headers, media_type=content_type)

    client = await get_http_client()
    upstream = await client.post(upstream_url, json=resp_payload, headers=headers)
    response_headers = filter_headers(dict(upstream.headers))
    response_headers.pop("content-length", None)

    content_type = upstream.headers.get("content-type", "application/json")
    if "application/json" in content_type:
        data = upstream.json()
    else:
        data = upstream.text

    tokens_delta = 0
    if upstream.status_code < 400 and isinstance(data, dict):
        usage = data.get("usage") or {}
        total = usage.get("total_tokens") or (
            (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
        )
        if total:
            tokens_delta = int(total)

    if upstream.status_code < 400:
        await usage_buffer.add(user.id, tokens=tokens_delta, requests=1)

    if isinstance(data, dict):
        return JSONResponse(content=build_chat_response(data), status_code=upstream.status_code, headers=response_headers)

    return Response(content=str(data), status_code=upstream.status_code, headers=response_headers, media_type=content_type)


@router.post("/embeddings")
async def embeddings(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        user = await authorize_request(request, db)
    except HTTPException as e:
        if e.status_code == 401:
            return openai_error("Invalid API key provided", "authentication_error", code="invalid_api_key", status_code=401)
        elif e.status_code == 403:
            return openai_error("Access denied", "permission_error", code="access_denied", status_code=403)
        elif e.status_code == 429:
            return openai_error(e.detail, "rate_limit_error", code="rate_limit_exceeded", status_code=429)
        raise

    try:
        payload = await request.json()
    except Exception:
        return openai_error("Invalid JSON payload", "invalid_request_error", code="invalid_json")

    if not isinstance(payload, dict):
        return openai_error("Request body must be a JSON object", "invalid_request_error")

    payload["model"] = settings.fixed_model
    payload.pop("model_provider", None)

    upstream_url = f"{settings.upstream_base_url.rstrip('/')}/embeddings"
    headers = {
        "api-key": settings.upstream_api_key,
        "content-type": "application/json",
    }

    client = await get_http_client()
    upstream = await client.post(upstream_url, json=payload, headers=headers)
    response_headers = filter_headers(dict(upstream.headers))
    response_headers.pop("content-length", None)

    content_type = upstream.headers.get("content-type", "application/json")
    if "application/json" in content_type:
        data = upstream.json()
    else:
        data = upstream.text

    tokens_delta = 0
    if upstream.status_code < 400 and isinstance(data, dict):
        usage = data.get("usage") or {}
        total = usage.get("total_tokens")
        if total:
            tokens_delta = int(total)

    if upstream.status_code < 400:
        await usage_buffer.add(user.id, tokens=tokens_delta, requests=1)

    if isinstance(data, dict):
        return JSONResponse(content=data, status_code=upstream.status_code, headers=response_headers)

    return Response(content=str(data), status_code=upstream.status_code, headers=response_headers, media_type=content_type)
