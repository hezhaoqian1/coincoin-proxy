---
type: guide
status: active
owner: platform
audience: [user, developer, operator, agent]
updated: 2026-07-28
canonical_for: enterprise-reporting-api-guide
---

# 如何使用企业余额与用量查询 API

这份指南用于已从 CoinCoin 管理员处取得企业 Reporting Key 的客户。完成后，
你可以查询管理员授权账号的当前可用余额、余额状态和最近 1 至 90 天的汇总用量。

该 API 是只读的拉取接口。CoinCoin 不会因为余额不足而主动发送飞书、钉钉或
邮件消息；如需提醒，请按本文的定时轮询示例接入你自己的告警系统。

## 准备工作

你需要：

- 一个以 `cc_ent_` 开头的企业 Reporting Key；
- `curl`；
- 可选的 `jq`，用于筛选 JSON 结果和实现余额检查。

生产环境 API 地址为：

```text
https://clawfather.up.railway.app
```

Reporting Key 只能访问本文列出的企业查询接口，不能调用模型、管理后台、充值
或用户管理接口。不要使用管理员 Token 或普通模型 API Key 代替它。

## 第一步：设置 Key

把 Key 放进环境变量，避免在每条命令中重复填写：

```bash
export COINCOIN_ENTERPRISE_API_KEY='<ENTERPRISE_REPORTING_KEY>'
```

在服务器或 CI 中，应通过 Secret Manager、密钥文件权限或部署平台的 Secret
变量注入，不要把真实 Key 写进源码、Git、镜像、工单、聊天记录或 URL。

## 第二步：查询所有授权账号的余额

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${COINCOIN_ENTERPRISE_API_KEY}" \
  -H 'Accept: application/json' \
  'https://clawfather.up.railway.app/v1/enterprise/balances' | jq
```

成功时返回 `200`。响应中的 `data` 每一项代表管理员授权给当前企业的一个账号：

```json
{
  "object": "enterprise.balance.list",
  "enterprise": {
    "code": "customer-code",
    "name": "Customer Name"
  },
  "currency": "usd_cents",
  "as_of": "2026-07-28T07:53:44Z",
  "total_available_balance_cents": 13838,
  "data": [
    {
      "account_code": "operator-one",
      "account_status": "active",
      "available_balance_cents": 14038,
      "available_balance_usd": 140.38,
      "balance_status": "ok",
      "last_activity_at": "2026-07-28T07:40:00Z"
    }
  ]
}
```

关键字段：

| 字段 | 含义 |
| --- | --- |
| `account_code` | 企业侧账号标识。管理员未单独填写时，它就是该账号的用户名。 |
| `available_balance_cents` | 当前可用余额，单位为整数美分；`100` 表示 `$1.00`。 |
| `available_balance_usd` | 同一余额的美元小数表示，方便展示。计费判断应优先使用美分整数。 |
| `balance_status` | `ok`、`low` 或 `insufficient`。 |
| `last_activity_at` | 最近一次请求时间；从未请求过时为 `null`。 |
| `total_available_balance_cents` | 本次响应中所有授权账号可用余额的合计。 |

`balance_status` 的判断规则是：

- 余额小于或等于 `0`：`insufficient`；
- 余额大于 `0`，但小于或等于管理员配置的低余额阈值：`low`；
- 其它情况：`ok`。

如果低余额阈值配置为 `0`，正余额不会出现 `low`，只有余额小于或等于 `0`
时才会出现 `insufficient`。

## 第三步：查询最近用量

`days` 可以是 `1` 至 `90` 的整数，省略时默认为 `7`：

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${COINCOIN_ENTERPRISE_API_KEY}" \
  -H 'Accept: application/json' \
  'https://clawfather.up.railway.app/v1/enterprise/usage-summary?days=7' | jq
```

成功响应包含：

- `period`：实际统计区间；
- `total`：全部授权账号的请求数、Token、图片、视频和成本合计；
- `data`：按 `account_code` 拆分的同类指标；
- `cost_cents`：整数美分成本；
- `cost_usd`：用于展示的美元金额。

该接口返回聚合数据，不返回请求正文、模型路由、渠道、普通 API Key、内部用户
ID 或原始请求日志。

## 定时检查余额并触发提醒

下面的脚本在存在 `low` 或 `insufficient` 账号时打印账号与余额，并以状态码 `2`
退出。你可以让现有监控捕获这个状态码，或在告警分支中调用飞书、钉钉、邮件
服务。脚本本身不会发送消息。

```bash
#!/usr/bin/env bash
set -euo pipefail

: "${COINCOIN_ENTERPRISE_API_KEY:?missing enterprise reporting key}"

response=$(curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${COINCOIN_ENTERPRISE_API_KEY}" \
  -H 'Accept: application/json' \
  'https://clawfather.up.railway.app/v1/enterprise/balances')

alerts=$(jq -c '[
  .data[]
  | select(.balance_status == "low" or .balance_status == "insufficient")
]' <<<"${response}")

if jq -e 'length == 0' <<<"${alerts}" >/dev/null; then
  exit 0
fi

jq -r '.[] | "\(.account_code): \(.balance_status), $\(.available_balance_usd)"' \
  <<<"${alerts}"
exit 2
```

建议每 5 分钟运行一次。告警系统应按 `account_code + balance_status` 去重，只在
状态从 `ok` 变为 `low/insufficient` 时通知，并在恢复为 `ok` 时发送恢复消息，
避免每次轮询都重复报警。

## Python 调用示例

下面的示例只依赖 Python 标准库：

```python
import json
import os
from urllib.request import Request, urlopen

key = os.environ["COINCOIN_ENTERPRISE_API_KEY"]
request = Request(
    "https://clawfather.up.railway.app/v1/enterprise/balances",
    headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    },
)

with urlopen(request, timeout=30) as response:
    payload = json.load(response)

for account in payload["data"]:
    print(
        account["account_code"],
        account["available_balance_cents"],
        account["balance_status"],
    )
```

## 验证

首次接入时完成以下检查：

1. 两个接口都返回 HTTP `200`。
2. `enterprise.code` 和 `enterprise.name` 是预期企业。
3. `data` 只包含双方确认过的授权账号。
4. 至少抽查一个账号的余额和后台计费视图一致。
5. 用量查询的 `period.days` 与请求的 `days` 一致。

余额已包含 CoinCoin 本地尚未写入数据库的待结算用量，因此极短时间内的结果
可能随正在处理的请求变化。`as_of` 表示本次响应的生成时间。

## 常见错误

| HTTP 状态 | 原因与处理 |
| --- | --- |
| `401` | Key 缺失、格式错误、不存在、已撤销或已过期。确认使用 `Bearer` 请求头；需要时联系管理员换发 Key。 |
| `403` | 企业已停用，或请求来源 IP 不在 Key 的白名单内。联系管理员核对企业状态和出口 IP。 |
| `422` | 参数校验失败。`days` 必须是 `1..90` 的整数。 |
| `429` | 当前 Key 超过查询频率限制。降低轮询频率，并使用退避重试。 |
| `500` | 服务端余额或数据库查询失败。不要使用部分结果，稍后重试并联系 CoinCoin 运维。 |

认证失败的响应不会说明 Key 是错误、过期还是已撤销，这是为了避免向未认证的
调用方泄露凭证状态。

## Key 安全与轮换

- 只通过 `Authorization: Bearer ...` 请求头发送 Key，绝不放进 URL 查询参数；
- 为生产、测试等不同用途签发不同 Key；
- 配置合理的过期时间，并在有固定出口 IP 时启用 IP 白名单；
- 日志中记录 Key 的管理端指纹，不记录明文；
- 怀疑 Key 泄露时，先签发并验证新 Key，再撤销旧 Key；已撤销 Key 不能恢复。

完整字段与边界请查阅 [Enterprise Reporting API 参考](../reference/enterprise-reporting-api.md)。
