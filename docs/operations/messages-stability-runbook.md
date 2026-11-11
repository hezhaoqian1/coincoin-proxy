---
type: runbook
status: active
owner: platform
audience: [operator, developer, agent]
updated: 2026-11-11
canonical_for: messages-stability-operations
---

# /v1/messages 稳定性与上游报错处理 Runbook

本文记录 2026-11-10 对 datayes「站点卡、超时、报错」问题的排查结论、已上线的修复、线上配置，以及后续怎么观察。不含任何密钥或 admin token。

## 1. 背景

datayes 在其网关（`aladdin-llm-proxy`，出口 IP 47.116.112.194，直连 `datayes.up.railway.app`）里跑 **Claude Agent SDK（Claude Code 内核）**，模型用 **gpt-6.1-sol**，走我们的 Anthropic 兼容入口 `/v1/messages`。他们网关的首字超时大约是 **60 秒**：60 秒内收不到任何字节就断开（我们这边的 Railway 日志记为 HTTP 499，`txBytes=0`）。网关里另有少量 GPT 请求走 `/v1/chat/completions`、`/v1/responses`。

## 2. 根因（2026-11-10 08:44–15:56 北京时间，datayes `/v1/messages` 1096 次）

| 类型 | 次数 | 根因 |
|---|---|---|
| 对话超出模型上下文（请求体 ≥2.5MB） | 65（同一对话反复重发，实际 14 次） | 上游把「超出上下文」包成 **502** 返回，我们当成通道故障，把 11 个通道全部试一遍（中位数 84 秒）。客户端 60 秒就断开，拿不到真实原因，也就不会去压缩上下文 |
| 0.5–2.5MB 请求卡住、60 秒内零字节 | 58 | 上游（主要是 Flow-Node）一直不出首字，而 `/v1/messages` 没有首字超时，会一直等下去。失败率随请求体变大明显上升：<500KB 1–2.5%，0.5–1MB 4.5%，1–2.5MB 22% |
| 首字之后流中途断开 | 15 | Flow-Node 流到一半失败。本轮**没有处理** |

几个容易误判的点：

- **不是 Anthropic 兼容层本身慢**：同样大小的上下文，`/v1/messages` 与主站 Codex 走的原生 `/v1/responses` 首字时间基本一样；成功请求即使 40 万+ token，首字 p50 也只有 14 秒、p90 23 秒。
- **不是探针造成的**：他们的探针每分钟约 5 个小请求，失败为 0。
- **他们的统计可信**：截图里 `firstTokenTimedOut` 约 5%，和我们这边「超限 + 首字超过 30 秒」合计约 4.8% 对得上。
- **不压缩上下文不是我们报错 token 用量导致的**：GPT 走兼容层时，`message_start` 里 input token 为 0，真实数字在结尾的 `message_delta` 里；Claude Code 的用量合并逻辑会取后面大于 0 的值，所以它能看到真实大小。真正原因是 Claude Code 不认识 GPT 模型的上下文窗口，需要客户自己设置 `CLAUDE_CODE_MAX_CONTEXT_TOKENS`；他们大概率设成了约 100 万，或关闭了自动压缩。
- **上传本身也慢**：从国内向 Railway 新加坡上传 4.4MB 实测约 44 秒（约 100KB/s），请求体越大越吃超时额度。

另外两类报错：

- `/v1/chat/completions`：我们把 chat 转成 Responses 格式时，原样复制消息，导致缺 `type`（lcodex 报 `Missing required parameter: 'input[0].type'`），还把客户的私有字段（`isCompactInternal`、`conversationRequestId`）带了出去，严格的上游报 `Unknown parameter`。
- `403 GROUP_DISABLED`「API Key 所属分组已停用」：来自旧的系统兜底上游 `sub2api.52ai.pro`。它的 Key 所在分组已停用，`legacy_auto` 模型在所有渠道都失败或冷却时会落到它上面。

## 3. 已上线的修复

| PR | 内容 |
|---|---|
| #69 | `/v1/messages` 识别「超出上下文」（不看状态码，看报错内容；规则对齐 sub2api 的 `isOpenAIContextWindowError`），**不切通道**，直接返回 Anthropic 格式的 `400 invalid_request_error "prompt is too long: …"`。不计通道失败，不告警，日志记 400。Claude Code 识别到 `prompt is too long` 会自动压缩上下文 |
| #70 | chat → Responses 转换只输出 `{"type":"message","role","content"}`，丢弃客户私有字段 |
| #71 | `legacy_auto` 模型在旧上游不可用（没配 URL 或 Key）时，改为忽略冷却去试渠道；完全没有路由才报错，绝不发往不可用的上游 |
| #72 | `/v1/messages` 流式请求加首字超时切换（见第 5 节） |
| #73 | `/v1/messages` 上游返回 ≥400 时，记录状态码和报错前 300 字节（日志关键字 `returned <status> on /v1/messages`） |
| #74 | 超限识别补充 `exceeds the context limit`（kedaya）和 `input is too long`；新增本文档 |

识别规则在 `app/upstream_errors.py` 的 `is_context_window_error`。JSON 报错只看结构化字段（`error.message`、`error.code`、`message`、`detail` 等），避免把回显的请求内容误判成超限。

## 4. 线上配置（两个站：`coincoin-proxy` 主站、`通联专属` datayes）

| 变量 | 值 | 说明 |
|---|---|---|
| `COINCOIN_UPSTREAM_FIRST_OUTPUT_TIMEOUT_SECONDS` | `30` | 2026-11-10 打开；`/v1/messages`、`/v1/responses`、chat 三条路径共用 |
| `COINCOIN_UPSTREAM_BASE_URL` / `COINCOIN_UPSTREAM_API_KEY` | **已删除** | 旧 52ai 兜底。必须两个一起删：只删 URL 会回落到 `config.py` 默认的 Azure 地址，并把旧 Key 发过去 |

注意：

- `railway variable delete/set` **不会自动重新部署**，改完要执行 `railway redeploy -s <service> -y`。
- 按模型单独覆盖：`COINCOIN_UPSTREAM_FIRST_OUTPUT_TIMEOUTS='{"<public_model_id>": <秒>}'`；高推理强度：`COINCOIN_UPSTREAM_HIGH_EFFORT_FIRST_OUTPUT_TIMEOUT_SECONDS`（对 high/xhigh 取两者较大值）。两者目前都没设。
- 选 30 秒的依据：主站过去 24 小时大多数流式请求只有 1–2% 首字超过 30 秒（responses medium 7.5%、messages high 5.8% 偏高）；datayes 的客户端 60 秒超时，30 秒切换后第二个通道还有约 30 秒。

## 5. 首字超时切换的行为（#72）

- 计时从向上游发请求开始，覆盖「等响应头」和「等第一个对客户端可见的事件」两个阶段。
- 超时：记一条 504 尝试记录（`request_log_only`，不计费）、记通道失败（`upstream_first_output_timeout`），切到下一个通道；下一次尝试的 `route_reason` 为 `channel_fallback:upstream_first_output_timeout`。
- **每个请求最多因超时切一次**，之后的通道不限时，避免大上下文的慢请求在各通道间来回浪费。没有通道可切时，继续等当前通道。
- 限制：「首个输出」指任何事件。有些渠道（如 ApiMars）2 秒内会先发一个空的开始事件，之后若卡在正文上，这个机制管不到。sub2api 等的是「首个语义输出」，判断更细。
- 2026-11-10 线上验证：在 datayes 上给 `gpt-6-sol` 临时设 1 秒门限，4 次请求中 3 次在 1.0 秒准时从 ApiMars 切到 Flow-Node，全部返回 200、答案正确；随后已删掉该临时门限。

同类方案对比：

- **sub2api**：只在原生 Responses 入口有 `openai_first_output_timeout_seconds`（默认 0），Anthropic 兼容入口和 chat 兼容入口没有；另有 15 秒无内容先发 `: ping` 心跳（代价是之后不能再切账号）、流中断 180 秒判超时。
- **new-api**：没有首字超时切换；可选 `: PING` 心跳（默认关闭，60 秒）；`STREAMING_TIMEOUT` 300 秒；只按状态码重试。

## 6. 怎么观察

只读 SQL 用各站 MySQL 服务的 `MYSQL_PUBLIC_URL`，配合 `SET SESSION TRANSACTION READ ONLY`。

- **超限快速返回**：`coincoin_request_logs` 中 `endpoint LIKE 'messages%' AND status_code=400 AND route_attempt=0`。应在几秒到十几秒内返回，后面不应再跟着同一对话的反复重发。
- **首字超时切换**：
  - `route_reason='channel_fallback:upstream_first_output_timeout'`：切换后的那次尝试，看它是否成功；
  - `status_code=504 AND request_log_only` 行：被放弃的那次尝试。
- **客户端放弃**：`railway logs -s 通联专属 --http --json --since <窗口> -f "@srcIp:47.116.112.194"`，统计 `/v1/messages` 的 499，并按 `rxBytes` 分档：<500KB、0.5–1MB、1–2.5MB、≥2.5MB。
- **上游原始报错**：`railway logs -s <service> --json --since <窗口> -f "\"on /v1/messages\""`。
- **客户端报 `405 {"detail":"Method Not Allowed"}`**：多半是 Base URL 多写了 `/v1`。例如 Claude 桌面端 Gateway base URL 填成 `…/v1`，实际发出的是 `POST /v1/v1/messages`（UA `claude-cli/… (external, claude-desktop-3p)`）。分支 `fix/api-path-v1-compat` 起，`app/api_path_compat.py` 会在路由前自动纠正多写或少写的 `/v1`；还出现 405 时，按 HTTP 日志里的 `path` 查是哪种新写法。
- **Railway 日志只覆盖当前这次部署**：`--since` 再长，也只能看到最近一次部署之后的日志。一天部署多次时，要尽快拉取；更早的记录去查 `coincoin_request_logs`。
- **链路追踪**：失败尝试的 `upstream_request_id` 以 `ccreq_<id>|<上游 id>` 开头。按 `ccreq_` 前缀分组即可还原整条切换链（`route_attempt` 为顺序）；经过 birdapi 的那一跳，可用后半段 id 去 sub2api 的 `ops_error_logs` 查上游原文。

预期效果（按 11-10 的数据推算）：datayes `/v1/messages` 客户端可见失败率从约 **12.6%** 降到 **3.5–4.7%**；不算会自愈的超限 400，是 2.2–3.5%。如果客户再把 `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 调到 30–40 万，大部分请求会落在 1MB 以下的低失败率区间。

## 7. 未解决 / 待跟进

- **统一Api GPT（tyapi）余额不足**：2026-11-10 返回 `403 INSUFFICIENT_BALANCE`；2026-11-11 探测已恢复正常。
- **渠道余额告警（2026-11-11 起）**：每个渠道的自动探测（约 5 分钟一次）只要拿到「余额/额度不足」的报错，或 HTTP 402，就立刻往告警群推送「上游渠道余额不足」，内容包括站点、渠道名、上游原文和处理建议。之后如果一直没恢复，每 6 小时再提醒一次（`COINCOIN_CHANNEL_BALANCE_ALERT_REPEAT_SECONDS`）；探测恢复正常时推送「余额已恢复」。识别规则在 `app/upstream_errors.py` 的 `is_insufficient_balance_error`。探测记录里这类失败显示为 `上游余额不足 HTTP <状态码>: <上游原文>`，不再只写 `HTTP 403`。告警复用后台「服务可靠性 → 告警」里的 Webhook 和开关，事件类别是 `balance`。
- **datayes 站的告警 Webhook 失效**：配的是钉钉机器人，2026-09-19 之后每次推送都返回 `errcode 300005`（token 不存在），所以 datayes 站的所有告警都没送达。需要换成有效的钉钉或企业微信机器人地址。
- **主站 Claude 渠道 2026-11-10 16:55–17:20（北京时间）整条链不稳**：
  - 首选 Yimo Claude MAX（`api.ymocode.com`，优先级 0）几乎全部 1 秒内返回 `503 No available accounts`（号池空了），16:50 起先出现 429；
  - 回落到 Sixoner ccmax 后，部分请求 30–60 秒不出字，超时后再切；
  - zzshu 一直返回 `403 Insufficient user quota`（余额 −116），**需要充值或停用**；
  - 可达鸭部分模型返回 400。
  整条链都失败时，客户端收到 502（datayes 回测 17:10–17:11 共 5 次，另有个别用户在 17:01–17:06 收到 503/504）。17:20 起 Yimo 恢复。
- **Flow-Node 遇到大请求体会返回 Cloudflare 502 HTML 页**（主站 `flow-node.com`），另有流中途断开（每天约 15 次）。
- **心跳尚未实现**：可以参考 sub2api，在首字超时切换之后再开始发 `: ping`。前提是先确认 datayes 网关是按「任何字节」还是按「首个正文」计时。
- `/v1/responses`、chat 两条路径遇到上下文超限，仍会切通道。
- 「首个输出」改成按语义判断（见第 5 节的限制）。
- 给 datayes 的建议：
  1. 设置 `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 为 30–40 万；
  2. 网关首字超时放宽到 120 秒以上；
  3. 不要把内部字段放进发给模型的消息里；
  4. 网关直接调 GPT 的请求可改用 `/v1/responses`（Claude Code 的流量继续走 `/v1/messages`）。
- **观察计划**：2026-11-11、11-12 各按第 6 节复查一次，对比失败率、切换次数和成功率、超限 400 次数，并据此决定是否调整 30 秒门限。
