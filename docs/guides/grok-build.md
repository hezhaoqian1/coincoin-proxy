---
type: guide
status: active
owner: platform
audience: [user, operator, developer, agent]
updated: 2026-08-25
canonical_for: grok-sixoner-upstream-setup
---

# Grok 与 Grok Build（Sixoner）

CoinCoin 对外提供 `grok-4.5`、`grok-4.6` 和 `grok-build`。三个公共模型都通过
后台 Provider Channel 路由到 Sixoner，不在模型目录中保存上游密钥。

## 渠道配置

在管理后台创建独立渠道，不复用或修改其他供应商渠道：

| 字段 | 值 |
| --- | --- |
| Name | `Sixoner Grok` |
| Provider platform | `sub2api` |
| Channel type | `openai_compatible` |
| Base URL | `https://sub.sixoner.com/v1` |
| Auth style | `bearer` |
| Status | `active` |
| Capabilities | `chat/completions,responses` |

API Key 只通过管理后台写入加密字段，不要提交到 Git、文档或示例环境变量。

为该渠道创建四条独立路由：

| Public model | Endpoint | Upstream model |
| --- | --- | --- |
| `grok-4.5` | `chat/completions` | `grok-4.5` |
| `grok-4.5` | `responses` | `grok-4.5` |
| `grok-build` | `chat/completions` | `grok-4.5` |
| `grok-build` | `responses` | `grok-4.5` |

为 `grok-4.6` 单独创建两条路由，均使用上游模型 `grok-4.6`：

| Public model | Endpoint | Upstream model |
| --- | --- | --- |
| `grok-4.6` | `chat/completions` | `grok-4.6` |
| `grok-4.6` | `responses` | `grok-4.6` |

`grok-build` 必须映射到 `grok-4.5`。Sixoner 的模型目录虽然会列出
`grok-build`，但实测将它作为 Responses 上游模型直接调用返回 HTTP 502。

## 已验证能力

2026-08-25 使用 Sixoner 生产入口验证：

- `GET /v1/models` 返回 HTTP 200，并列出 `grok-4.5`、`grok-4.6` 和 `grok-build`。
- `grok-4.5` 的 Chat Completions 返回 HTTP 200。
- `grok-4.5` 的 Responses 返回 HTTP 200。
- `grok-4.6` 已加入公共目录；上线前必须通过 Sixoner 渠道的 Chat Completions
  和 Responses 代表探针确认上游实际可用。
- Responses 平铺函数工具能返回 `function_call`。
- Sixoner HTTP Responses 不接受 `previous_response_id`，提示仅 Responses
  WebSocket v2 支持。CoinCoin 会从本地缓存展开上一轮输入和输出，并在请求上游前
  移除该字段，因此标准客户端工具回路仍可工作。

## 上线验证

先在后台执行渠道连接测试，确认模型列表包含 `grok-4.5`。然后分别通过 CoinCoin
请求两个公共模型：

```bash
curl https://coincoin.ai/v1/responses \
  -H "Authorization: Bearer $COINCOIN_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "grok-build",
    "input": "Reply exactly: COINCOIN_GROK_OK",
    "max_output_tokens": 32
  }'
```

上线检查只针对新建 Sixoner 渠道和上述四条 Grok 路由。不要调整其他渠道的状态、
优先级、权重、密钥、冷却状态或路由。
