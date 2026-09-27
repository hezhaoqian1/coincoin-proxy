# 多 worker 运行时（Codex / Claude 缓存友好）

CoinCoin 是 Python 服务，一个进程只能用满一个 CPU 核。镜像默认用
`uvicorn --workers ${WEB_CONCURRENCY:-4}` 启动多个进程。多进程之后，
**凡是只存在进程内存里的状态，其他 worker 都看不到**，所以需要共享的状态统一放进 Redis。
这和 sub2api / new-api 的做法一致：它们是 Go 单进程多核，扩容时多开实例，
所有实例共享数据库和 Redis。

## 共享了哪些状态

| 状态 | 不共享时的问题 | 共享方式 | 参考 |
|---|---|---|---|
| Responses `previous_response_id` 上下文（Codex） | 后续请求落到别的 worker 时找不到上下文，只能丢掉历史，只发送当前输入 | 两级缓存：L1 进程内 LRU，L2 Redis（zlib 压缩，绑定所属用户） | new-api HybridCache，sub2api response owner 绑定 |
| 上游渠道 / Gemini CPA 冷却 | 各 worker 对"哪个渠道在冷却"判断不一致，同一个 Codex/Claude 会话在渠道之间来回跳，prompt cache 失效 | 失败计数和冷却窗口用 Redis Lua 原子维护，每个 worker 每秒同步一次 | sub2api 账号调度状态 |
| API Key 缓存失效 | 吊销或修改 Key 只清掉了一个 worker 的缓存，其他 worker 最多还要等 30 秒 | Redis pub/sub 广播；重新订阅时清空本地缓存 | sub2api `auth:cache:invalidate` |
| 限流 | 每个用户的每分钟限额实际变成 N 倍 | 配置了 Redis 就自动使用 Redis 固定窗口计数 | new-api |
| 兜底耗尽告警 | 同一条告警发 N 次 | Redis `SET NX PX` 全局去重 | — |
| 支付对账 | 每个 worker 都去查一遍易支付 | Redis 主节点锁（带 token 校验和续期） | sub2api `leader:lock` |
| 启动 DDL 迁移 | 多个 worker 同时 ALTER TABLE，可能死锁或报错 | MySQL `GET_LOCK` 串行执行 | sub2api `pg_advisory_lock` |

渠道选择本身是**确定性的**：按用户、Key、模型、`prompt_cache_key` 做加权 rendezvous 哈希，
不依赖内存。所以只要各 worker 的冷却状态一致，同一个会话就会固定落在同一个渠道。

## 可用性设计

- **请求路径上不阻塞**：路由决策只读本地状态；冷却变化在后台异步写入 Redis。
- **超时加熔断**：连接在后台预热（建连预算 5 秒，绝不占用请求时间），每次操作默认最多等 0.5 秒；连续失败 3 次后熔断 10 秒，
  熔断期间完全不访问 Redis，直接使用进程内状态。
- **可以退化运行**：不配置 `COINCOIN_REDIS_URL` 时，行为和改造前完全一样。
- **Redis 恢复后自动补齐**：Redis 故障期间本地判定的冷却，会在恢复后重新写回 Redis。

## 上线配置

1. 在项目里加一个 Redis 服务，给 CoinCoin 设置 `COINCOIN_REDIS_URL=${{Redis.REDIS_URL}}`。
2. 检查限流相关变量：
   - `COINCOIN_REDIS_RATE_LIMITER_ENABLED`：删掉或留空（表示自动），也可以设为 `true`。
     **不要保留 `false`**，否则多 worker 下限额会变成 N 倍。
   - `COINCOIN_REDIS_RATE_LIMITER_FALLBACK_TO_LOCAL=true`：设为 `false` 时，
     Redis 一旦故障，所有请求都会被限流拒绝。
3. `WEB_CONCURRENCY` 默认是 4。改大之前先确认两件事：
   - MySQL `max_connections` 大于 `WEB_CONCURRENCY × (COINCOIN_DB_POOL_SIZE + 20)`；
   - Railway 服务的内存足够，每个 worker 都要单独占一份内存。
4. 如果没有部署 `usage-quota-service`，建议设置 `COINCOIN_USAGE_EVENT_SHADOW_ENABLED=false`。
   就算开着，事件流也会按 `COINCOIN_USAGE_EVENT_STREAM_MAXLEN` 裁剪，不会无限增长。

## 上线后怎么验证

- 启动日志里出现 `CoinCoin Proxy started worker=... redis_shared_state=on`，
  并且 `Started server process` 的行数等于 worker 数。
- `GET /admin/ops/health` 返回的 `runtime` 字段里：
  - `shared_state.redis_configured=true`，`redis_circuit_open=false`，`invalidation_bus_connected=true`；
  - `response_cache.l2_hits` 持续增长，说明 Codex 后续请求在跨 worker 找回上下文；
  - 注意：这里显示的是**处理这次请求的那个 worker** 的视图。
- 日志里 `polyfill: ... not in cache` 应该基本消失，取而代之的是
  `polyfill: restored ... from shared cache`。

## Redis 键

所有键都以 `COINCOIN_REDIS_KEY_PREFIX`（默认 `coincoin`）开头：

| 键 | 用途 | 过期 |
|---|---|---|
| `respconv:v1:<sha256>` | Responses 上下文 | `COINCOIN_RESPONSE_CACHE_TTL`（默认 300 秒） |
| `chanstate:v1:<ns>:cooldowns` | 冷却 ZSET（score 为结束时间，毫秒） | 过期成员自动清理 |
| `chanstate:v1:<ns>:fails:<channel>` | 失败计数 | 至少 10 分钟 |
| `bus:v1:invalidate` | pub/sub 频道 | — |
| `leader:v1:payment-reconcile` | 对账主节点锁 | 2 × 间隔 + 30 秒 |
| `once:v1:fallback-exhausted:<digest>` | 告警去重 | `fallback_alert_dedup_seconds` |
