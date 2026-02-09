# CoinCoin Proxy

OpenAI 兼容的 API 代理服务，将 Azure OpenAI Responses API 转换为标准 OpenAI Chat Completions 格式。

## 功能特性

- **OpenAI 兼容** - 完全兼容 OpenAI Chat Completions API 格式
- **Tools/Function Calling** - 支持工具调用，自动转换格式
- **用户管理** - 多用户 API Key 管理
- **余额计费** - 支持按 Input/Output Token 分别计费，实时扣费
- **用量统计** - 分项统计 Input/Output Token 和消费金额
- **限流控制** - 支持每分钟/每日请求限制
- **管理后台** - Web UI 管理界面
- **充值接口** - Webhook 充值，支持余额和 Token 额度

---

## 快速开始

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

# 上游 Azure OpenAI 配置
COINCOIN_UPSTREAM_BASE_URL=https://your-instance.cognitiveservices.azure.com/openai/v1
COINCOIN_UPSTREAM_API_KEY=your-azure-api-key
COINCOIN_FIXED_MODEL=gpt-4o

# 数据库配置 (MySQL/TiDB)
COINCOIN_DB_HOST=localhost
COINCOIN_DB_PORT=3306
COINCOIN_DB_NAME=coincoin
COINCOIN_DB_USER=root
COINCOIN_DB_PASSWORD=password

# 计费配置（可选）
COINCOIN_PRICE_INPUT_PER_MILLION=175    # Input 价格: 175 分/百万tokens = $1.75/M
COINCOIN_PRICE_OUTPUT_PER_MILLION=1400  # Output 价格: 1400 分/百万tokens = $14/M
COINCOIN_BILLING_MODE=balance           # 计费模式: balance(余额) / token_limit(额度) / none(不限制)

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

---

## API 文档

### 基础端点

| 端点 | 方法 | 描述 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/v1/models` | GET | 列出可用模型 |
| `/v1/models/{model_id}` | GET | 获取模型信息 |
| `/v1/balance` | GET | 查询账户余额和用量 |

### OpenAI 兼容端点

#### Chat Completions

```bash
POST /v1/chat/completions
Authorization: Bearer sk_cc_xxx
Content-Type: application/json

{
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
  ],
  "stream": false,
  "temperature": 0.7,
  "max_tokens": 1000
}
```

**响应：**

```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "created": 1234567890,
  "model": "gpt-4o",
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
  "price_input_per_million": 1.75,
  "price_output_per_million": 14.0
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
| `COINCOIN_PRICE_INPUT_PER_MILLION` | `175` | Input 价格（分/百万Token）|
| `COINCOIN_PRICE_OUTPUT_PER_MILLION` | `1400` | Output 价格（分/百万Token）|
| `COINCOIN_BILLING_MODE` | `balance` | 计费模式：balance/token_limit/none |
| `COINCOIN_WEBHOOK_SECRET` | - | Webhook 充值密钥 |

---

## 数据库

### 表结构

服务启动时会自动创建以下表：

- `coincoin_users` - 用户表（含余额和分项 Token 统计）
- `coincoin_api_keys` - API Key 表
- `coincoin_usage_daily` - 每日用量表（含分项统计和消费金额）
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

默认价格（与 OpenAI 对标）：

| 类型 | 价格 | 环境变量 |
|------|------|----------|
| Input Token | $1.75 / 百万 | `COINCOIN_PRICE_INPUT_PER_MILLION=175` |
| Output Token | $14.00 / 百万 | `COINCOIN_PRICE_OUTPUT_PER_MILLION=1400` |

> 注：价格单位为「分/百万Token」，175 分 = $1.75

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
费用(分) = ceil(input_tokens × 175 / 1000000 + output_tokens × 1400 / 1000000)
```

示例：
- 100 input + 50 output = ceil(0.0175 + 0.07) = 1 分 = $0.01
- 1000 input + 500 output = ceil(0.175 + 0.7) = 1 分 = $0.01
- 10000 input + 5000 output = ceil(1.75 + 7) = 9 分 = $0.09

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
