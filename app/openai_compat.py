import asyncio
import json
import secrets
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .proxy import authenticate_user, authorize_request, filter_headers, get_http_client, get_stream_client, proxy_responses, responses_health
from .schemas import BalanceResponse
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


@router.get("/balance", response_model=BalanceResponse)
async def get_balance(request: Request, db: AsyncSession = Depends(get_db)):
    """
    查询账户余额和使用量
    
    使用自己的 API Key 认证，返回余额、token 用量和价格信息。
    注：直接从数据库读取最新数据，不使用缓存。
    """
    try:
        cached_user = await authenticate_user(request, db)
    except HTTPException as e:
        if e.status_code == 401:
            return openai_error("Invalid API key provided", "authentication_error", code="invalid_api_key", status_code=401)
        elif e.status_code == 403:
            return openai_error("Access denied", "permission_error", code="access_denied", status_code=403)
        raise
    
    # 直接从数据库查询最新用户数据（不用缓存）
    from .models import User
    from sqlalchemy import select
    result = await db.execute(select(User).where(User.id == cached_user.id))
    user = result.scalar_one_or_none()
    if not user:
        return openai_error("User not found", "authentication_error", code="user_not_found", status_code=404)
    
    # 获取待刷新的数据
    pending_tokens = await usage_buffer.get_pending_tokens(user.id)
    pending_cost = await usage_buffer.get_pending_cost(user.id)
    
    # 计算当前值（数据库最新值 + 待刷新）
    current_balance = user.balance or 0
    balance = current_balance - pending_cost
    token_used = (user.token_used or 0) + pending_tokens
    input_tokens_used = user.input_tokens_used or 0
    output_tokens_used = user.output_tokens_used or 0
    token_limit = user.token_limit
    
    # 计算剩余 tokens
    token_remaining = None
    if token_limit is not None:
        token_remaining = max(0, token_limit - token_used)
    
    return BalanceResponse(
        user_id=user.id,
        balance=balance,
        balance_usd=balance / 100,  # 分转美元
        token_used=token_used,
        input_tokens_used=input_tokens_used,
        output_tokens_used=output_tokens_used,
        token_limit=token_limit,
        token_remaining=token_remaining,
        price_input_per_million=settings.price_input_per_million / 100,  # 分转美元
        price_output_per_million=settings.price_output_per_million / 100,  # 分转美元
    )


@router.get("/usage")
async def get_usage(
    request: Request,
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
    endpoint: Optional[str] = None,
    status_code: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    try:
        cached_user = await authenticate_user(request, db)
    except HTTPException as e:
        if e.status_code == 401:
            return openai_error("Invalid API key provided", "authentication_error", code="invalid_api_key", status_code=401)
        elif e.status_code == 403:
            return openai_error("Access denied", "permission_error", code="access_denied", status_code=403)
        raise

    from .models import RequestLog
    from sqlalchemy import select, func, and_
    from datetime import datetime as dt

    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    conditions = [RequestLog.user_id == cached_user.id]
    if endpoint:
        conditions.append(RequestLog.endpoint == endpoint)
    if status_code is not None:
        conditions.append(RequestLog.status_code == status_code)
    if start_date:
        try:
            conditions.append(RequestLog.created_at >= dt.fromisoformat(start_date))
        except ValueError:
            pass
    if end_date:
        try:
            conditions.append(RequestLog.created_at <= dt.fromisoformat(end_date))
        except ValueError:
            pass

    where = and_(*conditions)

    count_result = await db.execute(
        select(func.count()).select_from(RequestLog).where(where)
    )
    total = count_result.scalar() or 0

    result = await db.execute(
        select(RequestLog)
        .where(where)
        .order_by(RequestLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    logs = result.scalars().all()

    return {
        "user_id": cached_user.id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "data": [
            {
                "created_at": (log.created_at.isoformat() + "Z") if log.created_at else None,
                "endpoint": log.endpoint,
                "model": log.model,
                "input_tokens": log.input_tokens,
                "output_tokens": log.output_tokens,
                "total_tokens": log.input_tokens + log.output_tokens,
                "cost_cents": log.cost_cents,
                "cost_usd": log.cost_cents / 100,
                "duration_ms": log.duration_ms,
                "status_code": log.status_code,
            }
            for log in logs
        ],
    }


@router.get("/usage/daily")
async def get_daily_usage(request: Request, db: AsyncSession = Depends(get_db), days: int = 7):
    try:
        cached_user = await authenticate_user(request, db)
    except HTTPException as e:
        if e.status_code == 401:
            return openai_error("Invalid API key", "authentication_error", code="invalid_api_key", status_code=401)
        raise

    from .models import UsageDaily
    from sqlalchemy import select
    from datetime import date, timedelta

    days = max(1, min(days, 90))
    start = date.today() - timedelta(days=days - 1)

    result = await db.execute(
        select(UsageDaily)
        .where(UsageDaily.user_id == cached_user.id, UsageDaily.day >= start)
        .order_by(UsageDaily.day.asc())
    )
    rows = result.scalars().all()
    return [
        {
            "day": str(r.day),
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "tokens_total": r.tokens_total,
            "cost_cents": r.cost_cents,
            "cost_usd": r.cost_cents / 100,
            "requests_total": r.requests_total,
        }
        for r in rows
    ]


@router.post("/redeem")
async def redeem_code(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        cached_user = await authenticate_user(request, db)
    except HTTPException as e:
        if e.status_code == 401:
            return openai_error("Invalid API key", "authentication_error", code="invalid_api_key", status_code=401)
        raise

    from .models import RedemptionCode, User
    from .rate_limiter import rate_limiter
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from datetime import datetime as dt

    if not await rate_limiter.allow(f"redeem:{cached_user.id}", 6):
        raise HTTPException(status_code=429, detail="too many redeem attempts")

    body = await request.json()
    code_str = body.get("code", "").strip()
    if not code_str:
        raise HTTPException(status_code=400, detail="code is required")

    result = await db.execute(
        select(RedemptionCode)
        .where(RedemptionCode.code == code_str)
        .with_for_update()
    )
    code = result.scalar_one_or_none()
    if not code:
        raise HTTPException(status_code=404, detail="invalid redemption code")
    if code.status != "unused":
        raise HTTPException(status_code=400, detail="code already used or disabled")

    user = (
        await db.execute(select(User).where(User.id == cached_user.id).with_for_update())
    ).scalar_one()
    user.balance += code.balance_cents
    code.status = "used"
    code.used_by = user.id
    code.used_at = dt.utcnow()

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="concurrent redeem conflict, retry")

    return {
        "success": True,
        "added_cents": code.balance_cents,
        "new_balance": user.balance,
        "new_balance_usd": user.balance / 100,
        "message": "redemption successful",
    }


@router.get("/announcements")
async def list_announcements(db: AsyncSession = Depends(get_db)):
    from .models import Announcement
    from sqlalchemy import select

    result = await db.execute(
        select(Announcement)
        .where(Announcement.status == "active")
        .order_by(Announcement.created_at.desc())
        .limit(10)
    )
    anns = result.scalars().all()
    return [
        {
            "id": a.id,
            "title": a.title,
            "content": a.content,
            "priority": a.priority,
            "created_at": a.created_at.isoformat() + "Z" if a.created_at else None,
        }
        for a in anns
    ]


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
        compat_stream_t0 = time.monotonic()
        _compat_stream_usage = {"input": 0, "output": 0}

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

                    usage = event.get("usage") or (event.get("response") or {}).get("usage")
                    if usage:
                        _compat_stream_usage["input"] = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                        _compat_stream_usage["output"] = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                    
                    # 处理文本内容
                    if event_type in ("response.output_text.delta", "response.output_text.chunk"):
                        delta = event.get("delta")
                        if isinstance(delta, dict):
                            delta_text = delta.get("text")
                        else:
                            delta_text = delta if isinstance(delta, str) else None
                        if delta_text:
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
                pass
            finally:
                await upstream.aclose()
                if upstream.status_code < 400:
                    dur = int((time.monotonic() - compat_stream_t0) * 1000)
                    asyncio.create_task(usage_buffer.add(
                        user.id,
                        input_tokens=_compat_stream_usage["input"],
                        output_tokens=_compat_stream_usage["output"],
                        requests=1,
                        endpoint="chat/completions:stream",
                        model=settings.fixed_model,
                        duration_ms=dur,
                        status_code=upstream.status_code,
                    ))
        stream_headers = filter_headers(dict(upstream.headers))
        stream_headers.pop("content-length", None)
        stream_headers.setdefault("cache-control", "no-cache")
        stream_headers.setdefault("x-accel-buffering", "no")
        return StreamingResponse(iter_events(), status_code=upstream.status_code, headers=stream_headers, media_type=content_type)

    client = await get_http_client()
    t0 = time.monotonic()
    upstream = await client.post(upstream_url, json=resp_payload, headers=headers)
    duration_ms = int((time.monotonic() - t0) * 1000)
    response_headers = filter_headers(dict(upstream.headers))
    response_headers.pop("content-length", None)

    content_type = upstream.headers.get("content-type", "application/json")
    if "application/json" in content_type:
        data = upstream.json()
    else:
        data = upstream.text

    input_tokens_delta = 0
    output_tokens_delta = 0
    if upstream.status_code < 400 and isinstance(data, dict):
        usage = data.get("usage") or {}
        input_tokens_delta = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens_delta = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)

    if upstream.status_code < 400:
        await usage_buffer.add(
            user.id, input_tokens=input_tokens_delta, output_tokens=output_tokens_delta, requests=1,
            endpoint="chat/completions", model=settings.fixed_model, duration_ms=duration_ms, status_code=upstream.status_code,
        )

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
    t0 = time.monotonic()
    upstream = await client.post(upstream_url, json=payload, headers=headers)
    duration_ms = int((time.monotonic() - t0) * 1000)
    response_headers = filter_headers(dict(upstream.headers))
    response_headers.pop("content-length", None)

    content_type = upstream.headers.get("content-type", "application/json")
    if "application/json" in content_type:
        data = upstream.json()
    else:
        data = upstream.text

    input_tokens_delta = 0
    if upstream.status_code < 400 and isinstance(data, dict):
        usage = data.get("usage") or {}
        # Embeddings 只有 input tokens（prompt_tokens 或 total_tokens）
        input_tokens_delta = int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)

    if upstream.status_code < 400:
        await usage_buffer.add(
            user.id, input_tokens=input_tokens_delta, output_tokens=0, requests=1,
            endpoint="embeddings", model=settings.fixed_model, duration_ms=duration_ms, status_code=upstream.status_code,
        )

    if isinstance(data, dict):
        return JSONResponse(content=data, status_code=upstream.status_code, headers=response_headers)

    return Response(content=str(data), status_code=upstream.status_code, headers=response_headers, media_type=content_type)
