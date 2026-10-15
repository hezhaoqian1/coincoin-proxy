---
type: reference
status: active
owner: platform
audience: [developer, operator]
updated: 2026-10-08
canonical_for: context-cache-billing
---

# 长上下文与缓存计费

GPT 文本请求按输入总量选择价格档位，普通输入、缓存读取、缓存写入和输出分别计费。规则由公开模型配置编译一次，扣费、公开模型目录和后台价目共用该配置；上游路由模型名不会改变用户购买的公开模型价格。

## 档位与公式

默认支持的公开 ID：`gpt-5.5`、`gpt-5.6`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`、`gpt-6`、`gpt-6-astra`、`gpt-6-sol`、`gpt-6-luna`、`gpt-6.1-sol`。

| 输入总量 | 普通输入、缓存读取、缓存写入 | 输出 |
| --- | --- | --- |
| ≤ 272,000 tokens | 配置的标准单价 | 配置的标准单价 |
| > 272,000 tokens | 标准单价 × 2 | 标准单价 × 1.5 |

阈值包含缓存 tokens，不包含输出 tokens。超过阈值时**整次请求**切换价格，而非仅对超出的部分加价。272,000 仍是标准档位，272,001 才进入长上下文档位。

设输入总量为 I，缓存读取为 R，缓存写入为 W，输出为 O，档位对应的四项单价为 Pi、Pr、Pw、Po，则：

```text
普通输入 = I - R - W
费用（美分）= ((I - R - W) × Pi + R × Pr + W × Pw + O × Po) / 1,000,000
```

代码单价单位为美分/百万 tokens。费用先保留小数，沿用现有余额批量扣费时的美分取整规则；请求日志金额也沿用现有取整规则。

GPT-5.6 及后续这些模型的默认缓存写入倍率为 1.25；GPT-6.1 Sol 默认缓存读取倍率为 0.05，其他这些 GPT 模型沿用目录配置的 0.1。GPT-5.5 官方未列独立写入价，站内通用写入单价回退到普通输入价，不将其解释为官方独立写入费。

## 上游 usage 语义

- OpenAI Responses：`input_tokens` 已包含缓存读取和写入；分别读取 `input_tokens_details.cached_tokens`、`input_tokens_details.cache_write_tokens`。
- OpenAI Chat Completions：`prompt_tokens` 已包含缓存；读取对应 `prompt_tokens_details` 字段。
- Anthropic：顶层 `input_tokens` 不包含独立缓存计数，需要加上 `cache_read_input_tokens` 和 `cache_creation_input_tokens`。缺少写入总计时，兼容 `cache_creation.ephemeral_5m_input_tokens` 与 `ephemeral_1h_input_tokens` 的合计。

同一用量的兼容别名不重复相加，明确的写入 0 值优先于别名。各协议转换和生成的流式 usage 都保留写入计数。`UsageBuffer.add` 接收已经归一化的输入总量，不再猜测是否需要补加缓存。

费用计算将负数计数归零；若上游缓存计数之和超过输入总量，优先截断写入，再截断读取，防止产生负数普通输入或重复收费。这里保留 Anthropic 的现有单一写入单价，没有新增 5 分钟与 1 小时两种保留时长的差别定价。

## 配置与优先级

默认规则在 `app/token_pricing.py`，模型目录在 `config/model_catalog.json`。合并优先级为：内置公开 ID 默认规则 → catalog 的 `pricing` → 运行时价格覆盖。默认模型槽位也按解析后的公开 ID 获得缓存与档位规则，避免与显式同名条目去重时丢失配置。

可在某个模型的 `pricing` 中配置：

```json
{
  "context_pricing_tiers": [
    {
      "above_input_tokens": 272000,
      "input_multiplier": 2,
      "output_multiplier": 1.5
    }
  ]
}
```

Claude Haiku 5.5（`claude-haiku-5-5`）不在内置 GPT 默认规则中，而是在 catalog 里配置 `{"above_input_tokens": 100000, "input_multiplier": 5, "output_multiplier": 5}`：官方对超过 100,000 输入 tokens 的 prompt 整次按 $0.50 / $2.50（标准档 $0.10 / $0.50）计费，缓存读写同样 ×5。

`context_pricing_tiers: []` 显式关闭该模型的档位。支持多个阈值：选择输入量严格超过的最高阈值，倍率始终相对标准单价，不会逐档相乘。阈值必须为不重复的正整数，倍率必须为有限正数；错误配置会在编译时抛错。

现有模型倍率先形成标准单价，再应用档位倍率。用户缓存读取覆盖可以为 0，明确的免费单价不会被全局回退值覆盖。分站零售缓存单价按分站输入价和缓存倍率计算；分站批发费用使用主站公开价格及同一个上下文档位，不受用户缓存折扣影响。当前后台没有新增档位编辑表单，运营修改档位需要修改 catalog 后重新加载配置。

## API、日志和页面

- `/v1/models` 新增 `coincoin_context_pricing_basis: "whole_request"`、`coincoin_context_pricing_tiers`，继续提供标准档位的四项单价。
- 分站模型目录同样返回目标公开模型的上下文档位，并按分站零售输入价计算缓存读取和写入单价。
- `/admin/model-pricing` 对应返回 `context_pricing_basis`、`context_pricing_tiers`，并补充缓存写入单价和倍率。
- 请求日志新增 nullable JSON `pricing_details`，记录 `basis`、`tier`、命中阈值、两个档位倍率，以及 `input_per_million_cents`、`cache_read_per_million_cents`、`cache_write_per_million_cents`、`output_per_million_cents` 四项实际零售单价。现有 usage event 也保留该快照。
- `/v1/usage` 和后台用户请求日志接口返回这个快照。旧日志、图片/视频和显式费用覆盖请求可以为 null，前端兼容这些情况。
- 模型文档表格增加缓存写入列与各模型的阈值说明；用量日志在命中档位时显示“长上下文”。

新增列通过现有启动迁移框架添加：

```sql
ALTER TABLE coincoin_request_logs ADD COLUMN pricing_details JSON NULL;
```

发布时需要运行现有启动迁移并确认列存在；已有数据不回填，也不重算历史账单。本次仅完成本地实现，未连接生产库执行迁移或验证真实 MySQL DDL。

## 可复核示例

以 GPT-6.1 Sol 标准单价输入 $2、读取 $0.1、写入 $2.5、输出 $10 / 百万 tokens 为例：

| 用量 | 档位 | 费用 |
| --- | --- | --- |
| 100,000 输入全部为缓存写入，无输出 | 标准 | $0.25 |
| 300,000 普通输入，10,000 输出 | 长上下文 | $1.35 |
| 300,000 总输入，其中读取 100,000、写入 50,000；输出 10,000 | 长上下文 | $1.02 |

第三例普通输入 150,000 × $4/M = $0.60，读取 $0.02，写入 $0.25，输出 $0.15。

## 验证记录与边界

新增测试覆盖阈值两侧、整次请求切档、缓存进入阈值、输出不进入阈值、多档位、无效配置、免费缓存、协议转换、日志持久化参数、分站零售和批发，以及 Chat Completions/Responses × 流式/非流式四条接口的实际费用归集。

本地相关回归结果：391 passed、5 failed、83 subtests passed。5 个失败在未修改的 `299ed65b0bbaa0059ce89a34bf5606836e36ae16` 上逐项复现：

- Anthropic 的旧错误日志断言 1 项，以及使用已退役 `gpt-5.4-mini` 的测试 2 项。
- 后台 alias 测试 2 项，原因是 Windows 打开中的临时文件不允许再次读写。

前端生产构建通过。在本地编译产物、真实模型序列化和合成用量数据上，浏览器确认了缓存写入列、阈值说明、390px 手机视口下的表格横向滚动，以及用量页“长上下文”标记与 $1.02 示例账单；浏览器未记录控制台错误。

Windows 本地使用 Python 3.11，因项目固定的 asyncmy 0.2.10 在该环境需要编译器，测试环境使用 asyncmy 0.2.9 的可用 wheel；生产依赖文件未改动，测试没有发起真实数据库或推理请求。回归命令：

```powershell
$env:COINCOIN_DATABASE_URL='mysql://test@127.0.0.1:3306/test'
$env:PYTHONUTF8='1'
.\.venv-debug\Scripts\python.exe -m pytest tests/test_context_cache_billing.py tests/test_usage_buffer_units.py tests/test_model_catalog.py tests/test_station_reseller_runtime.py tests/test_openai_compat_defaults.py tests/test_anthropic_compat.py tests/test_responses_polyfill.py tests/test_usage_event_infra.py tests/test_admin_usage_fields.py tests/test_frontend_usage_filters.py -q --tb=short
npm --prefix coincoin-web run build
```

本次修复计费机制，基础价格仍使用现有配置。2026-10-08 审计发现 `gpt-5.6` / `gpt-5.6-sol` 的基础输入/输出 $5/$30 仍不同于官方当前促销价 $4/$20，需另行同步配置及部署环境。Fast、Flex、Batch 等服务层级价差、工具调用费和图片 token 定价也不在本次实现范围；不能据此宣称整个站点已完全复刻官方账单。

## 依据和参考实现

官方依据核对日期为 2026-10-08，后续价格调整应重新核对：

- [OpenAI API Pricing](https://developers.openai.com/api/docs/pricing)
- [GPT-6.1 Sol：长上下文阈值与整次请求倍率](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
- [Prompt Caching：读取、写入字段及价格](https://developers.openai.com/api/docs/guides/prompt-caching)
- [GPT-5.6 Sol：当前促销基础价格](https://developers.openai.com/api/docs/models/gpt-5.6-sol)

参考 sub2api 的整次请求档位、缓存读写独立计价与展示共用配置，参考 New API 的阈值条件表达式和四类 token 成本分开计算；实现使用本项目的 Python 计费链路，没有引入 Go 服务或表达式执行器：

- [sub2api context schedule](https://github.com/Wei-Shaw/sub2api/blob/3f1a2ea0a760730e3bc528105c00b4ee4f23e469/backend/internal/service/billing_context_schedule.go)
- [sub2api billing service](https://github.com/Wei-Shaw/sub2api/blob/3f1a2ea0a760730e3bc528105c00b4ee4f23e469/backend/internal/service/billing_service.go)
- [New API builtin billing](https://github.com/QuantumNous/new-api/blob/45094bdf41fb13a5d24bd3e8eb3fdfec6241c59f/setting/billing_setting/builtin_billing.go)
- [New API billing expressions](https://github.com/QuantumNous/new-api/blob/45094bdf41fb13a5d24bd3e8eb3fdfec6241c59f/pkg/billingexpr/expr.md)
