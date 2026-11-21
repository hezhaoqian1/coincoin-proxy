---
type: reference
status: active
owner: platform
audience: [developer, operator]
updated: 2026-11-22
canonical_for: gemini-pricing
---

# Gemini 4 / Gemini 3.8 模型定价与渠道（kedaya）

## 零售价：上游参考价的 3 倍

5 个模型都是 `route_only` 公共模型，基础价取 kedaya `/v1/models` 返回的 `reference_pricing`（Neon、Hydrogen、Carbon、3.8 Flash 与报道的 Google 当前价一致；Argon 是 12-01 起的正式价，见下文），catalog 里 `pricing.model_multiplier = 3`。

| 模型 | 基础价 输入 / 输出（$/1M） | 零售价 输入 / 输出（$/1M） | 缓存读取零售（$/1M） | 缓存读取倍率 |
|---|---|---|---|---|
| `gemini-4-neon` | 3 / 15 | **9 / 45** | 0.45 | 0.05 |
| `gemini-4-hydrogen` | 1.50 / 7.50 | **4.50 / 22.50** | 0.225 | 0.05 |
| `gemini-4-carbon` | 2 / 10 | **6 / 30** | 0.30 | 0.05 |
| `gemini-4-argon` | 4 / 20 | **12 / 60** | 0.60 | 0.05 |
| `gemini-3.8-flash` | 0.75 / 3.75 | **2.25 / 11.25** | 0.225 | 0.10 |

catalog 单位是「美分 / 百万 tokens」，例如 `gemini-4-neon` 写的是 `300 / 1500`，生效价 `900 / 4500`。

### 已知的官方调价（需要人工跟进）

第三方报道（未能直接打开 Google 定价页核对）：

- `gemini-4-carbon`：$2 / $10 是首发价，截至 2026-11-30；12-01 起官方价 $4 / $20。
- `gemini-4-argon`：首发价 $2 / $10 截至 2026-11-30，12-01 起 $4 / $20。kedaya 参考价已经是 $4 / $20，我们按 $4 / $20 定价。
- `gemini-3.8-flash`：$0.75 / $3.75 截至 2026-12-31，2027-01-01 起 $1.50 / $7.50。

线上调价不用发版：`PATCH /admin/model-pricing/<id>` 只能改倍率，比如 Carbon 基础价翻倍时，把 `model_multiplier` 改为 `6`（两个站都要改），零售价就保持在新官方价的 3 倍。

## 渠道与 route

| 站点 | 渠道 | ID |
|---|---|---|
| 主站 coincoin.ai | kedaya Gemini | `ch_b6e6a8c45b36eeba37d491ed` |
| datayes.coincoin.ai | kedaya Gemini | `ch_46a176ca16d5a2ae90787fb8` |

- `channel_type=openai_compatible`，`base_url=https://kedaya.ai/v1`，`auth_style=bearer`，capabilities `chat/completions,responses`
- 每个模型一条 route，`endpoint=""`（全部 endpoint），`upstream_model` 等于公共模型 id
- 只有这一个渠道，没有 fallback；新模型 id 没有 route 时请求直接报错，不会落到别的后端

客户端协议走向：

- `/v1/chat/completions` → 上游 `/v1/chat/completions`
- `/v1/responses` → 上游 `/v1/responses`（kedaya 原生支持）
- `/v1/messages`（Claude Code 等）→ 本地转换成 chat → 上游 `/v1/chat/completions`

## 上游协议测试（2026-11-22）

kedaya 这个 key 下只有这 5 个模型。每个模型 4 轮，全部 200，没有失败：

| 用例 | 结果 |
|---|---|
| OpenAI chat 非流式 / 流式（`include_usage`） | 20/20、20/20 |
| OpenAI responses 非流式 / 流式 | 20/20、20/20 |
| Gemini 原生 `v1beta/...:generateContent` | 20/20 |
| Anthropic `/v1/messages` | 20/20 |
| chat / responses 工具调用 | 5/5、5/5，参数正确 |
| chat 看图（base64 PNG） | 5/5，答对颜色 |

延迟大多 4–8 秒。Argon 走 responses 的 4 次里平均 31 秒，其它协议约 6 秒。

选 OpenAI chat 兼容渠道的原因：四种协议都稳定，chat 是我们网关里最通用的一条路径（chat 直传、messages 本地转 chat、responses 直传），用量字段也完整。

计费相关的观察：

- 缓存：chat 用量返回 `prompt_tokens_details.cached_tokens`（例：38,243 输入里 36,194 命中），网关按缓存读取价计费。
- 推理 token：上游不单独报告 thinking token，`completion_tokens` 只有可见输出，我们也只按这个计费。
- `reasoning_effort`：neon / hydrogen 支持 `low|medium|high|xhigh`，carbon / argon / 3.8-flash 支持 `low|medium|high`；`none`、`minimal` 上游返回 400。
- Gemini 4 系列每次请求比 3.8 Flash 多约 31 个输入 token（上游注入的固定前缀），金额可以忽略。
