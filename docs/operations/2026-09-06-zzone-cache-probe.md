# Zzone DeepSeek cache probe — 2026-09-06

## Scope

- Target: `https://zzone.cc.cd`, using the user-authorized credential.
- Completed: 2026-09-06 03:53:54 UTC / 11:53:54 Asia/Shanghai.
- One model-list request and 14 sequential generation requests, all HTTP 200.
- Synthetic reference text only; no repository contents or real conversations sent.
- Credential stayed in the execution kernel, was not written to files, and was cleared afterward.
- Output cap: 96 tokens per request. Observed output: 855 tokens, including reported reasoning.
- Sum of Chat prompt_tokens and Messages input_tokens: 61,141. This raw-counter sum is not an invoice or an independently verified unique-token count.
- Small diagnostic sample, not a production hit-rate benchmark. No deployment or account-setting changes.

## Request construction

Fixed system prefix: 23,209 characters, beginning with a random diagnostic tag followed by 120 deterministic synthetic reference lines. The tag reduces the chance of using a pre-existing prefix cache.

Prefix SHA-256: `a65a52330dff43105fc8dd57f5fc070396c12df3b09125157b11fbb30afa4ecc`.

Baseline Chat request:

```json
{
  "model": "deepseek-v4-pro",
  "messages": [
    {"role": "system", "content": "<fixed synthetic prefix>"},
    {"role": "user", "content": "Return OK."}
  ],
  "max_tokens": 96,
  "stream": false
}
```

The Messages variant uses the same text in a system text block with `cache_control: {"type": "ephemeral"}`, the same user text, and the same output cap. All three Messages bodies are identical. Bearer authorization was used; no Anthropic version header was supplied in this probe. The gateway accepted these requests.

The model list advertised `deepseek-v4-flash-0731`, `deepseek-v4-pro`, `deepseek-v4-pro-0813`, and `deepseek-v4-flash`. Only the unversioned aliases were tested. Returned labels do not independently prove the underlying model identity.

## Observations in request order

Chat input/cached columns use `prompt_tokens` and `prompt_tokens_details.cached_tokens`. Messages columns preserve raw `input_tokens` and `cache_read_input_tokens`; do not assume Chat semantics for these counters.

| # | Probe | Returned model | Raw input | Cached | Duration ms |
|---|---|---|---:|---:|---:|
| 1 | Pro Chat baseline, no key | deepseek-v4-pro | 4367 | 0 | 4933 |
| 2 | Exact repeat of #1 | deepseek-v4-pro | 4367 | 4096 | 4342 |
| 3 | Same system, changed final user text | deepseek-v4-pro | 4370 | 4096 | 4173 |
| 4 | Baseline plus prompt_cache_key | deepseek-v4-pro | 4367 | 0 | 4011 |
| 5 | Exact repeat of #4 | deepseek-v4-pro | 4367 | 4096 | 4769 |
| 6 | Baseline, key removed | deepseek-v4-pro | 4367 | 4096 | 3384 |
| 7 | Baseline, stream=true, no stream_options | deepseek-v4-pro | 4367 | 0 | 4815 |
| 8 | Stream with include_usage=true | deepseek-v4-pro | 4367 | 4096 | 4960 |
| 9 | Flash Chat baseline | deepseek-v4-flash | 4367 | 0 | 4001 |
| 10 | Exact repeat of #9 | deepseek-v4-flash-260425 | 4367 | 0 | 3816 |
| 11 | Pro Messages baseline | deepseek-v4-pro | 4367 | 4096 | 3178 |
| 12 | Exact repeat of #11 | deepseek-v4-pro-260425 | 4367 | 0 | 3704 |
| 13 | Exact repeat of #9 | deepseek-v4-flash | 4367 | 0 | 3442 |
| 14 | Exact repeat of #11 | deepseek-v4-pro | 4367 | 4096 | 3552 |

Both streaming probes returned a usage event and `[DONE]`. First received bytes: 3376 ms and 3405 ms, respectively; these are not necessarily first visible text-token timings.

## Findings and confidence

### Confirmed: a missing prompt_cache_key does not prevent hits

Pro Chat request #2 reported 4096/4367 cached/input tokens: **93.79%**, without a cache key. Changing only the user tail preserved the cached prefix. The earlier claim that missing cache keys were the confirmed root cause was incorrect.

Adding a key coincided with a miss, followed by a hit on repetition. This is consistent with key-sensitive routing or cache partitioning, but does not prove causality; backend movement and cache readiness are alternatives. Neither mandatory key support nor key ignorance has been established for this gateway.

### Confirmed: Flash reported no hits in this sample

Three identical Flash bodies reported zero cached tokens. The middle response changed its model label to `deepseek-v4-flash-260425`. This warrants investigation, but does not establish that Flash never caches or that the label identifies a different actual model.

### Confirmed: identical Messages requests have variable cache outcomes

Three identical Pro Messages bodies returned **4096 -> 0 -> 4096** cache-read tokens. The zero-hit response changed its model label to `deepseek-v4-pro-260425`.

Changing labels and request-ID formats indicate inconsistent gateway behavior. Multiple routes, adapters, or cache partitions are plausible, not verified backend topology. Only the upstream operator can map these request IDs to the actual channel/account/deployment.

### Strongly suspected: Messages input_tokens includes cached input

Messages request #11 returned:

```json
{
  "input_tokens": 4367,
  "cache_creation_input_tokens": 0,
  "cache_read_input_tokens": 4096,
  "output_tokens": 59,
  "claude_cache_creation_5_m_tokens": 0,
  "claude_cache_creation_1_h_tokens": 0
}
```

This duplicates the total input of the equivalent Chat request rather than reducing it by the cached input. Under the standard Anthropic interpretation, total input is regular input + cache read + cache creation. If the intended total is the same 4367 tokens, regular input should be 271, not 4367.

The gateway's final tokenized prompt and billing ledger are not visible. A counter-conversion defect is therefore strongly supported, not independently proven token-level equivalence.

## Local reproduction and conditional impact

Loaded the actual pure functions from `app/usage_buffer.py` via Python AST, without database imports. All three assertions passed:

| Usage shape | Local total | Cached | Cached/total |
|---|---:|---:|---:|
| Chat: prompt_tokens=4367, cached_tokens=4096 | 4367 | 4096 | 93.79% |
| Zzone Messages: input_tokens=4367, cache_read=4096 | 8463 | 4096 | 48.40% |
| Standard Messages equivalent: input_tokens=271, cache_read=4096 | 4367 | 4096 | 93.79% |

The local code follows standard Anthropic semantics. Globally changing the extractor would break compliant providers. If the upstream inclusive-input counter is confirmed, normalize only with an explicit provider/channel setting, or require the upstream converter to be fixed.

This affects CoinCoin only for Messages-shaped upstream usage. Claude Code calling CoinCoin `/v1/messages` does NOT automatically mean CoinCoin calls upstream Messages: `app/anthropic_compat.py` sends Chat for OpenAI-compatible channels and native Messages for Anthropic-compatible channels. Production database channel configuration was not inspected.

Chat usage from this target is supported locally through `prompt_tokens_details.cached_tokens`. These Chat probes did not return `prompt_cache_hit_tokens`; its absence does not mean cache usage was lost. No contradictory top-level zero/cache-details hit combination was observed.

## Request IDs for upstream support

| Probe | Gateway request ID |
|---|---|
| Pro Chat warm #2 | e4a22a45-0053-92be-87d0-6c31fb6b4e28 |
| Flash cold #9 | b17252e6-7244-9320-be32-05fa5cabb065 |
| Flash changed label #10 | 0217886667972727a1d4bf881c2833fb0e5e9d4efa0eb39303fd5 |
| Flash cold #13 | 4eeff873-15fb-921c-b864-0da1ce506959 |
| Pro Messages hit #11 | b673c454-6970-96e9-9929-4057fcce89e6 |
| Pro Messages changed label/miss #12 | 021788666804053160031606777e521370c7a15800bf67d665c46 |
| Pro Messages hit #14 | e68c3a48-64c5-938f-a6ee-3c91b3f2e1cd |

## Recommended next steps

1. Ask the operator to correlate these IDs with actual route, account, deployment, cache namespace, and model-label conversion. Local channel affinity cannot enforce routing behind the upstream gateway.
2. Ask whether every Flash route reports actual cached usage, including the route returning `-260425`.
3. Ask whether Messages input_tokens includes cached input and request a compliant conversion if it does.
4. Confirm production channel_type and actual outgoing endpoint before attributing dashboard discrepancies to the Messages counter mismatch.
5. Repeat a matched test against a genuinely pinned deployment if offered. Do not blindly switch to a versioned alias; those aliases were not validated.
6. Log outbound system/tools/history prefix hashes with observed response model, raw usage, route IDs, and request IDs. Do not log user prompts or credentials by default.
7. Rotate the temporary credential after the investigation.

## Corrections to earlier advice

- Retracted the speculative DeepSeek cache-key opt-ins, the unrelated Anthropic OpenAI-model opt-in, and the test introduced with those changes. Preserved the user's existing catalog/environment edits.
- Flattening blocks can lose Anthropic cache annotations, but that is not proof of a DeepSeek automatic-prefix-cache failure.
- A deterministic model cloak does not invalidate every repeated request. Its conditional application can change prefixes between request types, but real Claude Code tool-bearing conversations were not exercised here.
- Labels, request IDs, and cache counters are observations, not proof of model identity, physical cache location, or account architecture.

## Validation

- Six existing prompt-cache unit tests passed using the bundled Python 3.12 runtime.
- The three local usage-extraction assertions passed.
- Catalog JSON parses successfully; the report contains no API-key prefix.
- Application/test diffs introduced by the earlier speculative patch were removed. No production fix or deployment is claimed.
- Repository-wide diff checking still reports pre-existing catalog/environment whitespace and line-ending issues; these unrelated user edits were not cleaned up.
