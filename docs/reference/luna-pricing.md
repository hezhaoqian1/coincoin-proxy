---
type: reference
status: active
owner: platform
audience: [user, developer, operator]
updated: 2026-10-16
canonical_for: luna-pricing
---

# Luna 模型定价

`gpt-5.6-luna` 和 `gpt-6-luna` 的主站默认零售价为 OpenAI Standard 文本 token 价格的 **5 倍**。基础价格保留官方数值，`pricing.model_multiplier` 设为 `5`，输出附加倍率为 `1`。其他模型不受本次调整影响。

## 标准档价格

单位：USD / 百万 tokens。官方价格核对日期：2026-10-16。

| 模型 | 价格口径 | 输入 | 缓存读取 | 缓存写入 | 输出 |
| --- | --- | ---: | ---: | ---: | ---: |
| GPT-5.6 Luna | 官方标准价／调整前主站价 | $0.20 | $0.02 | $0.25 | $1.20 |
| GPT-5.6 Luna | CoinCoin 5 倍零售价 | **$1.00** | **$0.10** | **$1.25** | **$6.00** |
| GPT-6 Luna | 官方标准价／调整前主站价 | $0.10 | $0.01 | $0.125 | $0.50 |
| GPT-6 Luna | CoinCoin 5 倍零售价 | **$0.50** | **$0.05** | **$0.625** | **$2.50** |

官方来源：[GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)、[GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)。缓存读取为零售输入价的 `0.1`，缓存写入为零售输入价的 `1.25`；这两个倍率明确写入目录，不依赖全站缓存折扣。

## 长上下文

含缓存的输入总量 **> 272,000 tokens** 时，整次请求的输入及缓存价格再乘 `2`，输出价格乘 `1.5`。恰好 272,000 tokens 仍使用标准档。

| 模型 | 输入 | 缓存读取 | 缓存写入 | 输出 |
| --- | ---: | ---: | ---: | ---: |
| GPT-5.6 Luna | $2.00 | $0.20 | $2.50 | $9.00 |
| GPT-6 Luna | $1.00 | $0.10 | $1.25 | $3.75 |

这些价格仍为官方长上下文对应档位的 5 倍。计费公式、usage 归一化及分站规则见[长上下文与缓存计费](./context-cache-billing.md)。本表采用 Standard 口径；Fast、Flex、Batch、地域附加费及工具调用费不在此表内。已有用户缓存价格覆盖、分站独立零售价继续按各自配置执行。

## 配置与发布核对

`config/model_catalog.json` 的基础价格及 API 返回值使用**美分 / 百万 tokens**，不是美元。两款模型的基础输入／输出分别为 `20/120` 与 `10/50`；编译后零售输入／输出应为 `100/600` 与 `50/250`，缓存读取／写入应为 `10/125` 与 `5/62.5`。

1. 环境变量 `COINCOIN_GPT_56_LUNA_INPUT_PRICE`、`COINCOIN_GPT_56_LUNA_OUTPUT_PRICE`、`COINCOIN_GPT_6_LUNA_INPUT_PRICE`、`COINCOIN_GPT_6_LUNA_OUTPUT_PRICE` 表示**基础价**。不要将五倍零售价写入这些变量后再设置倍率，否则会重复加价。
2. 运行时 `/admin/model-pricing/{model_id}` 覆盖优先于目录。若存在旧覆盖，通过该接口 PATCH `{"model_multiplier":5,"output_multiplier":1,"cache_read_multiplier":0.1}`；缓存写入倍率 `1.25` 由目录继承。只有确认需要恢复全部目录默认值时才删除覆盖。
3. 确认部署使用已更新的目录；若设置了 `COINCOIN_MODEL_CATALOG_JSON` 或自定义目录路径，需同步相应配置。若将 Luna 设为 `COINCOIN_FIXED_MODEL`，也需配置默认槽位的基础价和倍率，避免同名默认槽位优先于显式目录项。
4. 发布后读取 `/admin/model-pricing/{model_id}` 与公开 `/v1/models`，核对基础价、倍率和四项零售价，并确认 `/health` 正常。数据库覆盖会替代目录倍率，不会与目录倍率重复相乘。

## 回归验证

```powershell
$env:COINCOIN_DATABASE_URL='mysql://test@127.0.0.1:3306/test'
$env:PYTHONUTF8='1'
.\.venv-debug\Scripts\python.exe -m pytest tests/test_model_catalog.py tests/test_context_cache_billing.py tests/test_usage_buffer_units.py tests/test_station_reseller_runtime.py -q --tb=short
```

2026-10-16：78 passed、64 subtests passed。新增用例从真实目录加载两款 Luna，核对公开和后台价目，并验证包含普通输入、缓存读取、缓存写入及输出的账单在 272,000 与 272,001 tokens 两侧均为官方价的五倍。测试使用合成用量，不发送付费推理请求。
