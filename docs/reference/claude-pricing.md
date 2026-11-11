---
type: reference
status: active
owner: platform
audience: [developer, operator]
updated: 2026-11-11
canonical_for: claude-pricing
---

# Claude 模型定价

所有公开 Claude 模型的零售价统一为**目录基础价的 4 倍**（`model_multiplier = 4`，`output_multiplier = 1`），主站和 datayes 专属站一致。范围包括 `claude-*`，以及 Claude Code 别名 `best`、`default`、`opus[1m]`、`sonnet[1m]`、`opusplan`。缓存读取和缓存写入按各模型自己的缓存倍率，在 4 倍零售输入价上计算。

Claude 价格以本文为准。[Claude Code Upstream Runbook](../operations/claude-code-upstream-runbook.md) 只讲渠道和路由。

## 价格表

单位：USD / 百万 tokens。2026-11-11 从两站 `/admin/model-pricing` 读取，两站数值相同。

| 模型 | 基础输入 / 输出 | 零售输入 | 缓存读取 | 缓存写入 | 零售输出 |
| --- | --- | ---: | ---: | ---: | ---: |
| `claude-fable-5-2`（部署后生效） | $8 / $40 | **$32** | $0.80 | $40 | **$160** |
| `claude-fable-5-1` | $10 / $50 | **$40** | $1.00 | $50 | **$200** |
| `claude-fable-5` | $10 / $50 | **$40** | $4.00 | $40 | **$200** |
| `claude-opus-5-5` | $4 / $20 | **$16** | $0.80 | $20 | **$80** |
| `claude-opus-5` | $5 / $25 | **$20** | $2.00 | $25 | **$100** |
| `claude-opus-4-8`、`claude-opus-4.8`、`claude-opus-4-7`、`claude-opus-4.7`、`claude-opus-4-6`、`claude-opus-4.6`、`claude-opus-4.5`、`best`、`default`、`opus[1m]`、`opusplan` | $5 / $25 | **$20** | $2.00 | $20 | **$100** |
| `claude-sonnet-5-5` | $2 / $10 | **$8** | $0.80 | $10 | **$40** |
| `claude-sonnet-5`、`claude-sonnet-4-6`、`claude-sonnet-4.6`、`claude-sonnet-4.5`、`claude-sonnet-4`、`sonnet[1m]` | $3 / $15 | **$12** | $1.20 | $12 | **$60** |
| `claude-haiku-5-5` | $0.10 / $0.50 | **$0.40** | $0.04 | $0.50 | **$2** |
| `claude-haiku-4-5`、`claude-haiku-4.5`、`claude-haiku-4-5-20251001` | $1 / $5 | **$4** | $0.40 | $4 | **$20** |

`claude-haiku-5-5` 含缓存的输入总量超过 100,000 tokens 时，整次请求再 ×5（零售 $2 / $10），见[长上下文与缓存计费](./context-cache-billing.md)。官方价格来源：[Anthropic 定价](https://platform.claude.com/docs/en/about-claude/pricing)。

### 已知差异（未改，待定）

下面几项与「官方价 × 4」不一致。这些是 2026-11-11 之前就存在的配置，这次统一倍率时没有改动：

- `claude-sonnet-5` 及表中同行的 Sonnet 别名使用 $3 / $15 基础价（`COINCOIN_CLAUDE_SONNET_*`，Sonnet 4.6 的官方价）。官方 Sonnet 5 是 $2 / $10。
- `claude-sonnet-5-5` 的缓存读取倍率是 0.1。官方 Sonnet 5.5 的缓存读取是 $0.10，即 0.05 倍，按 4 倍应为 $0.40。
- Opus 4.x 别名、Sonnet 5 / 4.x、Haiku 4.5、`claude-fable-5` 的缓存写入倍率是 1.0（缓存写入 = 零售输入价）。官方 5 分钟缓存写入是 1.25 倍。

## 倍率从哪里来

零售价 = 基础价 × 倍率。基础价和倍率分开配置：

1. **基础价**：存在 `config/model_catalog.json`，单位是**美分 / 百万 tokens**，可以用 `COINCOIN_CLAUDE_*_PRICE` 环境变量覆盖。这些变量只能填官方基础价。如果填零售价再乘倍率，就会重复加价。
2. **倍率**：后台覆盖（`/admin/model-pricing/{model_id}`，存在各站自己数据库的 `coincoin_model_pricing_overrides` 表里）优先；没有覆盖时用目录里的 `pricing.model_multiplier`。两者不会叠乘。

目前只有 `claude-sonnet-5-5`、`claude-haiku-5-5`、`claude-fable-5-2` 在目录里写了 `model_multiplier: 4.0`，其余 Claude 模型的目录默认值是 1。**这些模型的 4 倍完全来自后台覆盖。** 删除它们的覆盖（`DELETE /admin/model-pricing/{model_id}`）会让零售价掉回官方原价。

主站和 datayes 是两个独立的数据库，后台覆盖要分别改。目录里写的倍率会同时作用于两站。

## 新增 Claude 模型

1. 在目录的 `pricing` 里写 `"model_multiplier": 4.0, "output_multiplier": 1.0`，以及该模型官方的 `cache_read_multiplier` 和 `cache_creation_multiplier`。这样部署后两站自动是 4 倍，不需要再 PATCH。
2. 基础价用独立的环境变量名（例如 `COINCOIN_CLAUDE_FABLE_52_*`），不要和价格不同的旧型号共用。
3. 把新模型加进本文价格表。
4. 部署后按下面的「核对」检查两站。

## 修改倍率

```bash
curl -X PATCH "https://<站点>/admin/model-pricing/<model_id>" \
  -H "x-admin-token: $ADMIN_TOKEN" -H "content-type: application/json" \
  -d '{"model_multiplier": 4}'
```

- 站点：主站 `clawfather.up.railway.app`（coincoin.ai），datayes `datayes.up.railway.app`（datayes.coincoin.ai）。Cloudflare 在国内不稳定时直接用 Railway 域名。
- PATCH 只修改请求里给出的字段，缓存倍率等其他字段保持原值。
- 模型 id 里的方括号要做 URL 编码：`opus%5B1m%5D`、`sonnet%5B1m%5D`。
- 只调整个别模型价格时，在本文写明原因，避免以后被当成错误改回去。

## 核对

公开 `/v1/models` 不需要 token，返回生效后的倍率和单价：

```bash
for host in clawfather.up.railway.app datayes.up.railway.app; do
  curl -s "https://$host/v1/models" | python -c "
import json, sys
models = json.load(sys.stdin)['data']
claude = [m for m in models if any(s in (m['id'] + str(m.get('coincoin_billable_sku'))).lower() for s in ('claude', 'opus', 'sonnet', 'haiku', 'fable'))]
print('$host', len(claude), 'Claude models; not 4x:', [(m['id'], m.get('coincoin_model_multiplier')) for m in claude if m.get('coincoin_model_multiplier') != 4.0])"
done
```

两站都应输出 `not 4x: []`。

## 变更记录

- **2026-11-11**：统一为 4 倍。主站 `claude-opus-5-5` 从 5 倍改为 4 倍（降价），`opusplan` 从 1 倍改为 4 倍；datayes 全部 26 个 Claude 模型从 3.5 倍改为 4 倍。新增 `claude-fable-5-2`，目录自带 4 倍。只修改了 `model_multiplier`，缓存倍率和基础价没有变化。
- 旧版 `docs/architecture/claude-code-upstream-runbook.md` 中记录的「生产倍率 6.0」已经作废。
