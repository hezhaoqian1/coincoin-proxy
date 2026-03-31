# CoinCoin Proxy

OpenAI 兼容的 API 控制平面，负责客户密钥、余额与用量控制，并把公开模型目录路由到旧 GPT 链路或内部 LiteLLM gateway。当前长期架构里，Gemini 文本和 Gemini 图片都统一走 gateway，CoinCoin 只保留控制面职责。

## 功能特性

- **OpenAI 兼容** - 完全兼容 OpenAI Chat Completions API 格式
- **公开模型目录** - `/v1/models` 暴露受控模型别名而不是单一固定模型
- **Tools/Function Calling** - 支持工具调用，自动转换格式
- **Embeddings** - 支持 `/v1/embeddings`，并固定走 Azure `text-embedding-3-small`
- **用户管理** - 多用户 API Key 管理
- **余额计费** - 支持按 Input/Output Token 分别计费，实时扣费
- **图片能力** - 支持 `/v1/images/generations` 和 `/v1/images/edits`，并为图片请求记录独立 usage unit
- **用量统计** - 分项统计 Input/Output Token 和消费金额
- **限流控制** - 支持每分钟/每日请求限制
- **管理后台** - Web UI 管理界面
- **充值接口** - Webhook 充值，支持余额和 Token 额度

---

## 快速开始

完整环境变量说明与价格变量说明，统一以仓库根目录文档为准：

- [CoinCoin Env Reference](/Users/hezhaoqian/Desktop/codex_transfer_station/docs/operations/env-reference-coincoin.md)
- [Billing and Pricing Ops](/Users/hezhaoqian/Desktop/codex_transfer_station/docs/operations/billing-and-pricing-ops.md)

### 1. 安装依赖

```bash
cd coincoin-proxy
pip install -r requirements.txt
```

### 2. 配置环境变量

复制示例配置并修改：

```bash
cp env.example .env
```

编辑 `.env` 文件：

```env
# Admin Token (用于管理后台)
COINCOIN_ADMIN_TOKEN=your-admin-token

# 旧 GPT 链路（默认公共模型）
COINCOIN_UPSTREAM_BASE_URL=https://your-instance.cognitiveservices.azure.com/openai/v1
COINCOIN_UPSTREAM_API_KEY=your-azure-api-key
COINCOIN_FIXED_MODEL=gpt-5.2-codex
COINCOIN_EMBEDDING_MODEL=text-embedding-3-small
COINCOIN_MODEL_CATALOG_PATH=config/model_catalog.json

# 内部 LiteLLM gateway（Gemini text + Gemini images）
# 注意：这里填 gateway 根地址，不要带结尾斜杠；catalog 会自动补 /v1
COINCOIN_GATEWAY_BASE_URL=https://transfer-station-litellm-gateway-production.up.railway.app
COINCOIN_GATEWAY_API_KEY=your-internal-gateway-key
COINCOIN_GATEWAY_AUTH_STYLE=bearer

# 可选：直连 Vertex 调试 / fallback
# 不是公网 Gemini 图片主链路的必需项
COINCOIN_VERTEX_API_KEY=your-vertex-api-key
COINCOIN_VERTEX_GEMINI_API_BASE=https://aiplatform.googleapis.com/v1/publishers/google

# 多图异步任务
COINCOIN_IMAGE_JOBS_ENABLED=true
COINCOIN_IMAGE_JOB_SYNC_INPUT_LIMIT=2
COINCOIN_IMAGE_JOB_ASYNC_MAX_INPUTS=8
COINCOIN_IMAGE_JOB_MAX_TOTAL_BYTES=52428800

# 数据库配置 (MySQL/TiDB)
COINCOIN_DB_HOST=localhost
COINCOIN_DB_PORT=3306
COINCOIN_DB_NAME=coincoin
COINCOIN_DB_USER=root
COINCOIN_DB_PASSWORD=password

# 计费配置（可选）
COINCOIN_PRICE_INPUT_PER_MILLION=99     # Input 价格: 99 分/百万tokens = $0.99/M
COINCOIN_PRICE_OUTPUT_PER_MILLION=699   # Output 价格: 699 分/百万tokens = $6.99/M
COINCOIN_BILLING_MODE=balance           # 计费模式: balance(余额) / token_limit(额度) / none(不限制)

# Gemini 计费（可选；不填时默认 0）
COINCOIN_GEMINI_BALANCED_INPUT_PRICE=0
COINCOIN_GEMINI_BALANCED_OUTPUT_PRICE=0
COINCOIN_GEMINI_FAST_INPUT_PRICE=0
COINCOIN_GEMINI_FAST_OUTPUT_PRICE=0
COINCOIN_GEMINI_REASONING_INPUT_PRICE=0
COINCOIN_GEMINI_REASONING_OUTPUT_PRICE=0
COINCOIN_GEMINI_IMAGE_PRICE=0

# Webhook 密钥（用于充值接口）
COINCOIN_WEBHOOK_SECRET=your-webhook-secret
```

### 3. 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

或使用 reload 模式开发：

```bash
uvicorn app.main:app --reload --port 8000
```

## 模型兼容规则

- 终端客户只看 CoinCoin 的公开模型目录，不直接感知 LiteLLM 或 Vertex 的内部模型名
- 老用户如果不传 `model`，仍然走默认 GPT 公共模型
- 旧 GPT lane 当前公开 alias 包括 `gpt-5`、`gpt-5.1`、`gpt-5.1-codex`、`gpt-5.1-codex-mini`、`gpt-5.1-codex-max`、`gpt-5.2`、`gpt-5.2-codex`、`gpt-5.3-codex`、`gpt-5.4-mini`、`gpt-5-codex`、`gpt-5-codex-mini`，以及由 `COINCOIN_FIXED_MODEL` 指定的默认 GPT alias
- embedding 请求不再复用旧 GPT / CPA lane；`/v1/embeddings` 默认和显式 `text-embedding-3-small` 都直连 Azure
- 新增 Gemini 文本能力是增量暴露；显式传入 Gemini 文本 alias 时，会真实路由到内部 LiteLLM gateway
- Gemini 图片 alias 的公网生产链路默认也走内部 LiteLLM gateway
- 图片模型支持 `/v1/images/generations` 与 `/v1/images/edits`，不会伪装成文本模型
- `<=2` 张输入图继续走同步 `/v1/images/edits`
- `>=3` 张输入图使用显式异步 job 端点，避免把大图任务强塞进同步公开契约
- 公开目录的 source of truth 是 `config/model_catalog.json`
- 后续扩模型时，按 [Add Public Model Runbook](/Users/hezhaoqian/Desktop/codex_transfer_station/docs/operations/add-public-model-runbook.md) 同步修改 LiteLLM、CoinCoin、测试与文档
- 每次发布后的统一验收，按 [Release Verification Checklist](/Users/hezhaoqian/Desktop/codex_transfer_station/docs/operations/release-verification-checklist.md) 执行

---

## API 文档

当前实际部署并对外使用的文档/API 入口只有一层：

- `coincoin-proxy` 自己的 README
- `coincoin-proxy` 站点里的 `/docs`

当前状态请按下面理解：

- Railway 上真正部署的是 `coincoin-proxy`
- 根仓库里的 `services/docs-portal/**`、`docs/**` 目前不是线上入口，不要当成已部署文档站
- 如果你要改线上用户实际看到的文档、示例和 API 说明，优先改 `coincoin-proxy` 这个嵌套仓库

注意：

- 当前这个嵌套仓库的 GitHub remote 名仍然是 `hezhaoqian1/clawfather`
- 但部署服务、代码目录和日常沟通都统一按 `coincoin-proxy` 理解，不要把 remote 名和 Railway 服务名混为一谈

### 基础端点

| 端点 | 方法 | 描述 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/v1/models` | GET | 列出公开模型目录 |
| `/v1/models/{model_id}` | GET | 获取模型信息 |
| `/v1/embeddings` | POST | 生成 embedding，固定走 Azure `text-embedding-3-small` |
| `/v1/balance` | GET | 查询账户余额和用量 |
| `/v1/usage` | GET | 查询请求明细（支持分页） |
| `/v1/images/generations` | POST | 生成图片 |
| `/v1/images/edits` | POST | 编辑图片 / 图生图 |
| `/v1/image-jobs/edits` | POST | 创建异步多图图生图任务 |
| `/v1/image-jobs/{job_id}` | GET | 查询异步图片任务状态和结果 |

### 图片编辑示例

```bash
POST /v1/images/edits
Authorization: Bearer sk_cc_xxx
Content-Type: multipart/form-data
```

```bash
curl https://<coincoin-domain>/v1/images/edits \
  -H "Authorization: Bearer sk_cc_xxx" \
  -F "model=gemini-image" \
  -F "prompt=Turn this into a clean pixel-art icon" \
  -F "n=1" \
  -F "size=1024x1024" \
  -F "image=@./input.png"
```

当前 Gemini 图生图说明：

- 支持 `multipart/form-data` 上传图片
- 1-2 张输入图：继续使用同步 `/v1/images/edits`
- 3-8 张输入图：改用 `/v1/image-jobs/edits`
- `n` 当前只支持 `1`
- 当前不支持 `mask` 上传；若传入 `mask`，会返回 `mask_not_supported`

### 多图异步图生图示例

```bash
curl https://<coincoin-domain>/v1/image-jobs/edits \
  -H "Authorization: Bearer sk_cc_xxx" \
  -F "model=gemini-image" \
  -F "prompt=Combine these references into one cohesive poster illustration" \
  -F "n=1" \
  -F "size=1024x1024" \
  -F "image=@./input-1.png" \
  -F "image=@./input-2.png" \
  -F "image=@./input-3.png"
```

然后轮询：

```bash
curl https://<coincoin-domain>/v1/image-jobs/<job_id> \
  -H "Authorization: Bearer sk_cc_xxx"
```

### OpenAI 兼容端点

#### Chat Completions

```bash
POST /v1/chat/completions
Authorization: Bearer sk_cc_xxx
Content-Type: application/json

{
  "model": "gemini-fast",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
  ],
  "stream": false,
  "temperature": 0.7,
  "max_tokens": 1000
}
```

#### Embeddings

```bash
POST /v1/embeddings
Authorization: Bearer sk_cc_xxx
Content-Type: application/json

{
  "model": "text-embedding-3-small",
  "input": "memory chunk to index"
}
```

说明：

- `text-embedding-3-small` 是当前公开 embedding alias
- `/v1/embeddings` 默认也会回落到这个模型
- embedding 请求固定走 Azure，不走旧 GPT / CPA cheap lane

**响应：**

```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "created": 1234567890,
  "model": "gemini-fast",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you?"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 10,
    "total_tokens": 30
  }
}
```

#### Tools / Function Calling

```bash
POST /v1/chat/completions
Authorization: Bearer sk_cc_xxx
Content-Type: application/json

{
  "messages": [
    {"role": "user", "content": "查一下北京天气"}
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "获取城市天气",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {"type": "string", "description": "城市名"}
          },
          "required": ["city"]
        }
      }
    }
  ],
  "tool_choice": "auto"
}
```

**响应：**

```json
{
  "id": "chatcmpl-xxx",
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_xxx",
            "type": "function",
            "function": {
              "name": "get_weather",
              "arguments": "{\"city\":\"北京\"}"
            }
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ]
}
```

#### 发送 Tool 结果

```bash
POST /v1/chat/completions

{
  "messages": [
    {"role": "user", "content": "查一下北京天气"},
    {
      "role": "assistant",
      "content": null,
      "tool_calls": [{"id": "call_xxx", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\":\"北京\"}"}}]
    },
    {
      "role": "tool",
      "tool_call_id": "call_xxx",
      "content": "北京今天晴天，气温25度"
    }
  ]
}
```

#### 流式响应

```bash
POST /v1/chat/completions

{
  "messages": [{"role": "user", "content": "Hello"}],
  "stream": true
}
```

返回 Server-Sent Events (SSE) 格式。

#### Image Generations

```bash
POST /v1/images/generations
Authorization: Bearer sk_cc_xxx
Content-Type: application/json

{
  "model": "gemini-image",
  "prompt": "A clean product illustration of a blue coin mascot on white background",
  "n": 1,
  "size": "1024x1024"
}
```

## 模型目录规则

- 老用户如果不传 `model`，仍然走默认 GPT 公共模型
- 新用户可以通过修改 `model` 在公开目录中切换
- 公开目录的 source of truth 是 `config/model_catalog.json`
- Gemini text 通过内部 LiteLLM gateway 提供
- Gemini 图片公网生产链路通过 CoinCoin 控制面直连 Vertex，不直接暴露 gateway master key 给终端客户

### 余额查询

#### 查询账户余额和用量

```bash
GET /v1/balance
Authorization: Bearer sk_cc_xxx
```

**响应：**

```json
{
  "user_id": "u_xxx",
  "balance": 1498,
  "balance_usd": 14.98,
  "token_used": 24876,
  "input_tokens_used": 57,
  "output_tokens_used": 41,
  "token_limit": null,
  "token_remaining": null,
  "price_input_per_million": 0.99,
  "price_output_per_million": 6.99
}
```

**字段说明：**

| 字段 | 类型 | 描述 |
|------|------|------|
| `balance` | int | 账户余额（分，1 cent = $0.01） |
| `balance_usd` | float | 账户余额（美元） |
| `token_used` | int | 已用 Token 总量 |
| `input_tokens_used` | int | 已用输入 Token |
| `output_tokens_used` | int | 已用输出 Token |
| `token_limit` | int/null | Token 限额（null 表示无限） |
| `token_remaining` | int/null | 剩余 Token（null 表示无限） |
| `price_input_per_million` | float | 输入价格（$/百万 Token） |
| `price_output_per_million` | float | 输出价格（$/百万 Token） |

### 请求明细查询

#### 查询 API 调用历史

```bash
GET /v1/usage?limit=50&offset=0
Authorization: Bearer sk_cc_xxx
```

**参数：**

| 参数 | 类型 | 默认 | 描述 |
|------|------|------|------|
| `limit` | int | 50 | 返回条数（1-200） |
| `offset` | int | 0 | 偏移量（分页） |

**响应：**

```json
{
  "user_id": "u_xxx",
  "total": 42,
  "limit": 50,
  "offset": 0,
  "data": [
    {
      "created_at": "2026-02-11T03:16:09",
      "endpoint": "chat/completions",
      "model": "gpt-5.2",
      "input_tokens": 12,
      "output_tokens": 12,
      "total_tokens": 24,
      "cost_cents": 1,
      "cost_usd": 0.01,
      "duration_ms": 1535,
      "status_code": 200
    }
  ]
}
```

**字段说明：**

| 字段 | 类型 | 描述 |
|------|------|------|
| `total` | int | 该用户的总记录数 |
| `data[].created_at` | string | 请求时间 (ISO 8601) |
| `data[].endpoint` | string | 调用端点（chat/completions, responses, embeddings） |
| `data[].model` | string | 使用的模型 |
| `data[].input_tokens` | int | 输入 Token 数 |
| `data[].output_tokens` | int | 输出 Token 数 |
| `data[].total_tokens` | int | 总 Token 数 |
| `data[].cost_cents` | int | 费用（分） |
| `data[].cost_usd` | float | 费用（美元） |
| `data[].duration_ms` | int | 响应耗时（毫秒） |
| `data[].status_code` | int | 上游状态码 |

### 用户管理端点

#### 创建用户 / 激活 Key

```bash
POST /v1/keys/activate
Content-Type: application/json

{
  "username": "alice"
}
```

或使用 external_id：

```json
{
  "external_id": "user_12345"
}
```

**响应：**

```json
{
  "user_id": "u_xxx",
  "api_key": "sk_cc_xxx",
  "status": "active"
}
```

### 管理后台端点

需要 Admin Token 认证：`Authorization: Bearer {admin_token}`

| 端点 | 方法 | 描述 |
|------|------|------|
| `/admin/ui` | GET | Web 管理界面 |
| `/admin/users` | GET | 用户列表 |
| `/admin/users/{user_id}` | GET | 用户详情 |
| `/admin/users/{user_id}` | PATCH | 更新用户 |
| `/admin/users/{user_id}/keys` | POST | 为用户创建新 Key |
| `/admin/keys/{key_id}` | PATCH | 更新 Key 状态 |
| `/admin/usage/daily` | GET | 每日用量统计 |
| `/admin/metrics/summary` | GET | 汇总指标 |
| `/admin/recharges` | GET | 充值记录 |

#### 更新用户（含余额）

```bash
PATCH /admin/users/{user_id}
Authorization: Bearer {admin_token}
Content-Type: application/json

{
  "status": "active",
  "balance": 10000,
  "token_limit": 1000000,
  "token_used": 0,
  "input_tokens_used": 0,
  "output_tokens_used": 0,
  "request_limit_per_minute": 60,
  "request_limit_per_day": 1000
}
```

**响应：**

```json
{
  "id": "u_xxx",
  "username": "alice",
  "status": "active",
  "balance": 10000,
  "token_limit": 1000000,
  "token_used": 0,
  "input_tokens_used": 0,
  "output_tokens_used": 0,
  "request_limit_per_minute": 60,
  "request_limit_per_day": 1000
}
```

### 充值接口 (Webhook)

用于外部支付系统回调充值。

```bash
POST /webhook/recharge
Authorization: Bearer {webhook_secret}
Content-Type: application/json

{
  "order_id": "order_123456",
  "user_id": "u_xxx",
  "amount": 1000,
  "add_balance": 1000,
  "add_tokens": 0,
  "add_daily_requests": 0,
  "note": "用户充值 $10"
}
```

**参数说明：**

| 参数 | 类型 | 必填 | 描述 |
|------|------|------|------|
| `order_id` | string | 是 | 订单号（用于幂等） |
| `user_id` | string | 是 | 用户 ID |
| `amount` | int | 否 | 支付金额（分） |
| `add_balance` | int | 否 | 增加余额（分），默认 0 |
| `add_tokens` | int | 否 | 增加 Token 额度，默认 0 |
| `add_daily_requests` | int | 否 | 增加每日请求限额，默认 0 |
| `note` | string | 否 | 备注 |

**响应：**

```json
{
  "success": true,
  "order_id": "order_123456",
  "user_id": "u_xxx",
  "balance": 11000,
  "token_limit": 1000000,
  "request_limit_per_day": 1000,
  "message": "recharge success"
}
```

### 直接代理端点

直接透传到 Azure Responses API（无格式转换）：

```bash
POST /openai/v1/responses
Authorization: Bearer sk_cc_xxx
Content-Type: application/json

{
  "model": "gpt-4o",
  "input": [{"role": "user", "content": "Hello"}],
  "stream": false
}
```

---

## 配置说明

### 环境变量

| 变量 | 默认值 | 描述 |
|------|--------|------|
| `COINCOIN_ADMIN_TOKEN` | `change-me` | 管理后台认证 Token |
| `COINCOIN_UPSTREAM_BASE_URL` | - | Azure OpenAI API 地址 |
| `COINCOIN_UPSTREAM_API_KEY` | - | Azure OpenAI API Key |
| `COINCOIN_FIXED_MODEL` | `gpt-5.2-codex` | 固定使用的模型名 |
| `COINCOIN_MODEL_CATALOG_PATH` | `config/model_catalog.json` | 公开模型目录配置文件 |
| `COINCOIN_GATEWAY_BASE_URL` | - | 内部 LiteLLM gateway 根地址 |
| `COINCOIN_GATEWAY_API_KEY` | - | 内部 LiteLLM gateway 访问密钥 |
| `COINCOIN_GATEWAY_AUTH_STYLE` | `bearer` | 访问内部 gateway 的认证方式 |
| `COINCOIN_DB_HOST` | - | 数据库主机 |
| `COINCOIN_DB_PORT` | `3306` | 数据库端口 |
| `COINCOIN_DB_NAME` | - | 数据库名 |
| `COINCOIN_DB_USER` | - | 数据库用户 |
| `COINCOIN_DB_PASSWORD` | - | 数据库密码 |
| `COINCOIN_DB_POOL_SIZE` | `10` | 连接池大小 |
| `COINCOIN_KEY_PREFIX` | `sk_cc_` | API Key 前缀 |
| `COINCOIN_KEY_PEPPER` | `coincoin-pepper` | Key 哈希盐值 |
| `COINCOIN_USAGE_FLUSH_INTERVAL` | `5` | 用量写入间隔(秒) |
| `COINCOIN_HTTP_POOL_MAX` | `100` | HTTP 连接池大小 |
| `COINCOIN_KEY_CACHE_TTL` | `30` | Key 缓存 TTL(秒) |
| `COINCOIN_PRICE_INPUT_PER_MILLION` | `99` | Input 价格（分/百万Token）|
| `COINCOIN_PRICE_OUTPUT_PER_MILLION` | `699` | Output 价格（分/百万Token）|
| `COINCOIN_BILLING_MODE` | `balance` | 计费模式：balance/token_limit/none |
| `COINCOIN_WEBHOOK_SECRET` | - | Webhook 充值密钥 |

## 本地验证

### 单元测试

```bash
cd /Users/hezhaoqian/Desktop/codex_transfer_station

env \
  PYTHONPATH=coincoin-proxy \
  PYTHONPYCACHEPREFIX=/tmp/pycache \
  COINCOIN_DB_HOST=localhost \
  COINCOIN_DB_NAME=test \
  COINCOIN_DB_USER=test \
  COINCOIN_DB_PASSWORD=test \
  python3 -m unittest discover -s coincoin-proxy/tests -p 'test_*.py'
```

### Live Vertex E2E

先启动本地 LiteLLM gateway，再运行可选 live 测试：

```bash
cd /Users/hezhaoqian/Desktop/codex_transfer_station

env \
  PYTHONPATH=coincoin-proxy \
  PYTHONPYCACHEPREFIX=/tmp/pycache \
  COINCOIN_RUN_LIVE_VERTEX_TESTS=1 \
  COINCOIN_LIVE_GATEWAY_URL='http://127.0.0.1:4010' \
  COINCOIN_LIVE_GATEWAY_KEY='replace-with-internal-gateway-key' \
  python3 -m unittest discover -s coincoin-proxy/tests -p 'test_live_coincoin_vertex_gateway.py'
```

这组 live 测试会：

- 真实请求 `coincoin-proxy -> local LiteLLM -> Vertex`
- 使用 checked-in `config/model_catalog.json`
- mock 掉 CoinCoin 的 DB/auth 依赖，因此不要求本地 MySQL

---

## 数据库

### 表结构

服务启动时会自动创建以下表：

- `coincoin_users` - 用户表（含余额和分项 Token 统计）
- `coincoin_api_keys` - API Key 表
- `coincoin_usage_daily` - 每日用量表（含分项统计和消费金额）
- `coincoin_request_logs` - 请求明细日志表（每次 API 调用记录）
- `coincoin_recharge_logs` - 充值记录表

### 手动创建表（可选）

```sql
CREATE TABLE coincoin_users (
    id VARCHAR(32) PRIMARY KEY,
    username VARCHAR(128) UNIQUE,
    external_id VARCHAR(128) UNIQUE,
    status VARCHAR(16) DEFAULT 'active',
    balance BIGINT DEFAULT 0 COMMENT '余额（分）',
    token_limit BIGINT,
    token_used BIGINT DEFAULT 0,
    input_tokens_used BIGINT DEFAULT 0 COMMENT '已用输入tokens',
    output_tokens_used BIGINT DEFAULT 0 COMMENT '已用输出tokens',
    request_limit_per_minute BIGINT,
    request_limit_per_day BIGINT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE coincoin_api_keys (
    id VARCHAR(32) PRIMARY KEY,
    user_id VARCHAR(32),
    key_hash VARCHAR(64) UNIQUE,
    status VARCHAR(16) DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used_at DATETIME,
    FOREIGN KEY (user_id) REFERENCES coincoin_users(id)
);

CREATE TABLE coincoin_usage_daily (
    user_id VARCHAR(32),
    day DATE,
    tokens_total BIGINT DEFAULT 0,
    input_tokens BIGINT DEFAULT 0 COMMENT '输入tokens',
    output_tokens BIGINT DEFAULT 0 COMMENT '输出tokens',
    cost_cents BIGINT DEFAULT 0 COMMENT '消费金额（分）',
    requests_total BIGINT DEFAULT 0,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, day),
    FOREIGN KEY (user_id) REFERENCES coincoin_users(id)
);

CREATE TABLE coincoin_request_logs (
    id VARCHAR(32) PRIMARY KEY,
    user_id VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    endpoint VARCHAR(64) NOT NULL DEFAULT '' COMMENT '调用端点',
    model VARCHAR(64) NOT NULL DEFAULT '' COMMENT '模型名称',
    input_tokens BIGINT NOT NULL DEFAULT 0 COMMENT '输入tokens',
    output_tokens BIGINT NOT NULL DEFAULT 0 COMMENT '输出tokens',
    cost_cents BIGINT NOT NULL DEFAULT 0 COMMENT '费用（分）',
    duration_ms BIGINT NOT NULL DEFAULT 0 COMMENT '响应耗时（毫秒）',
    status_code BIGINT NOT NULL DEFAULT 200 COMMENT '上游状态码',
    INDEX idx_user_created (user_id, created_at DESC),
    FOREIGN KEY (user_id) REFERENCES coincoin_users(id)
);

CREATE TABLE coincoin_recharge_logs (
    id VARCHAR(32) PRIMARY KEY,
    order_id VARCHAR(128) UNIQUE COMMENT '订单号',
    user_id VARCHAR(32),
    amount BIGINT COMMENT '支付金额（分）',
    balance_added BIGINT DEFAULT 0 COMMENT '增加的余额（分）',
    tokens_added BIGINT DEFAULT 0 COMMENT '增加的token额度',
    daily_requests_added BIGINT DEFAULT 0 COMMENT '增加的每日请求限额',
    note VARCHAR(256),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES coincoin_users(id)
);
```

### 数据库迁移（已有表）

如果已有旧表，执行以下 SQL 添加新字段：

```sql
-- 用户表新增字段
ALTER TABLE coincoin_users ADD COLUMN balance BIGINT NOT NULL DEFAULT 0 COMMENT '余额（分）';
ALTER TABLE coincoin_users ADD COLUMN input_tokens_used BIGINT NOT NULL DEFAULT 0 COMMENT '已用输入tokens';
ALTER TABLE coincoin_users ADD COLUMN output_tokens_used BIGINT NOT NULL DEFAULT 0 COMMENT '已用输出tokens';

-- 每日用量表新增字段
ALTER TABLE coincoin_usage_daily ADD COLUMN input_tokens BIGINT NOT NULL DEFAULT 0 COMMENT '输入tokens';
ALTER TABLE coincoin_usage_daily ADD COLUMN output_tokens BIGINT NOT NULL DEFAULT 0 COMMENT '输出tokens';
ALTER TABLE coincoin_usage_daily ADD COLUMN cost_cents BIGINT NOT NULL DEFAULT 0 COMMENT '消费金额（分）';

-- 充值记录表新增字段
ALTER TABLE coincoin_recharge_logs ADD COLUMN balance_added BIGINT NOT NULL DEFAULT 0 COMMENT '增加的余额（分）';
```

---

## 部署

### Docker

```bash
docker build -t coincoin-proxy .
docker run -d -p 8000:8000 \
  -e COINCOIN_ADMIN_TOKEN=xxx \
  -e COINCOIN_UPSTREAM_BASE_URL=xxx \
  -e COINCOIN_UPSTREAM_API_KEY=xxx \
  -e COINCOIN_DB_HOST=xxx \
  -e COINCOIN_DB_NAME=xxx \
  -e COINCOIN_DB_USER=xxx \
  -e COINCOIN_DB_PASSWORD=xxx \
  coincoin-proxy
```

### Railway

1. 连接 GitHub 仓库
2. 设置环境变量
3. 自动部署

Railway 会使用 `railway.toml` 配置和 `Dockerfile` 构建。

### 其他平台

支持任何能运行 Python 的平台：
- Fly.io
- Render
- Heroku
- AWS ECS
- GCP Cloud Run

---

## 客户端配置

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk_cc_xxx",
    base_url="https://clawfather.up.railway.app/v1"
)

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

### Codex CLI

```toml
# ~/.codex/config.toml
model = "gpt-4o"
model_provider = "openai"

[model_providers.openai]
name = "CoinCoin Proxy"
base_url = "https://clawfather.up.railway.app/v1"
env_key = "COINCOIN_API_KEY"
```

```bash
export COINCOIN_API_KEY="sk_cc_xxx"
codex
```

### curl

```bash
curl https://clawfather.up.railway.app/v1/chat/completions \
  -H "Authorization: Bearer sk_cc_xxx" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello"}]}'
```

---

## 格式转换说明

本代理将 OpenAI Chat Completions 格式转换为 Azure Responses API 格式：

### Messages 转换

| OpenAI 格式 | Azure 格式 |
|-------------|------------|
| `{"role": "assistant", "tool_calls": [...]}` | `{"type": "function_call", "call_id": "...", ...}` |
| `{"role": "tool", "tool_call_id": "...", "content": "..."}` | `{"type": "function_call_output", "call_id": "...", "output": "..."}` |
| `content: null` | `content: ""` |

### Tools 转换

| OpenAI 格式 | Azure 格式 |
|-------------|------------|
| `{"type": "function", "function": {"name": "x", ...}}` | `{"type": "function", "name": "x", ...}` |

### 参数映射

| OpenAI | Azure |
|--------|-------|
| `max_tokens` | `max_output_tokens` |
| `max_completion_tokens` | `max_output_tokens` |

---

## 错误处理

所有错误返回标准 OpenAI 错误格式：

```json
{
  "error": {
    "message": "Invalid API key provided",
    "type": "authentication_error",
    "param": null,
    "code": "invalid_api_key"
  }
}
```

### 常见错误码

| 状态码 | 类型 | 描述 |
|--------|------|------|
| 401 | `authentication_error` | API Key 无效 |
| 402 | `payment_required` | 余额不足 |
| 403 | `permission_error` | 用户被封禁 |
| 429 | `rate_limit_error` | 超出限额 |
| 400 | `invalid_request_error` | 请求格式错误 |
| 500 | `server_error` | 服务器内部错误 |

---

## 计费系统

### 计费模式

通过 `COINCOIN_BILLING_MODE` 配置：

| 模式 | 描述 |
|------|------|
| `balance` | 按余额扣费（默认） |
| `token_limit` | 按 Token 额度限制 |
| `none` | 不限制 |

### 价格配置

默认价格：

| 类型 | 价格 | 环境变量 |
|------|------|----------|
| Input Token | $0.99 / 百万 | `COINCOIN_PRICE_INPUT_PER_MILLION=99` |
| Output Token | $6.99 / 百万 | `COINCOIN_PRICE_OUTPUT_PER_MILLION=699` |

> 注：价格单位为「分/百万Token」，99 分 = $0.99

### 计费流程

```
1. 用户发起请求
2. 检查余额是否充足
3. 请求转发到上游 API
4. 获取 usage（input_tokens, output_tokens）
5. 计算费用并暂存到内存 buffer
6. 每 5 秒批量写入数据库（flush）
```

### 费用计算

```
费用(分) = round(input_tokens × 99 / 1000000 + output_tokens × 699 / 1000000)
```

示例：
- 100 input + 50 output = round(0.0099 + 0.035) = 0 分 = $0.00
- 1000 input + 500 output = round(0.099 + 0.35) = 0 分 = $0.00
- 10000 input + 5000 output = round(0.99 + 3.495) = 4 分 = $0.04

---

## 项目结构

```
coincoin-proxy/
├── app/
│   ├── __init__.py
│   ├── main.py           # 入口文件
│   ├── config.py         # 配置管理
│   ├── db.py             # 数据库连接
│   ├── models.py         # SQLAlchemy 模型
│   ├── schemas.py        # Pydantic 模型
│   ├── proxy.py          # 核心代理逻辑
│   ├── openai_compat.py  # OpenAI 兼容层
│   ├── admin.py          # 管理后台
│   ├── keys.py           # Key 管理
│   ├── webhook.py        # 充值 Webhook
│   ├── security.py       # 安全工具
│   ├── rate_limiter.py   # 限流器
│   ├── usage_buffer.py   # 用量缓冲（含计费）
│   └── static/
│       └── admin.html    # 管理界面
├── requirements.txt
├── Dockerfile
├── railway.toml
├── env.example
└── README.md
```

---

## License

MIT
