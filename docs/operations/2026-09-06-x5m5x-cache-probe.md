# api.x5m5x.com DeepSeek cache probe — 2026-09-06

## Scope

- Target: `https://api.x5m5x.com`, using the user-authorized credential.
- Credential was held only in the temporary execution kernel, not written to files, and cleared after the probe.
- No repository contents or real conversations were sent. Requests used synthetic system text only.
- All completed requests returned HTTP 200. No production routing or provider settings were changed.

## Model discovery

The model list advertised:

- `deepseek-v4-flash-0731`
- `deepseek-v4-pro-0813`
- `deepseek-v4-flash-vision-exp`

The tested aliases were `deepseek-v4-pro-0813` and `deepseek-v4-flash-0731`.

## Results

The short prefix was approximately 1769 tokens. The long prefix was approximately 4169 tokens. Each model received the same prefix twice or more through Chat Completions.

| Probe | Returned model | Prompt tokens | Cached tokens | Duration ms |
|---|---|---:|---:|---:|
| Pro short #1 | deepseek-v4-pro | 1769 | 0 | 4775 |
| Pro short #2 | deepseek-v4-pro | 1769 | 0 | 3802 |
| Pro short #3 | deepseek-v4-pro | 1769 | 0 | 2769 |
| Flash short #1 | deepseek-v4-flash-0731 | 1769 | 0 | 3336 |
| Flash short #2 | deepseek-v4-flash-0731 | 1769 | 0 | 2230 |
| Pro long #1 | deepseek-v4-pro | 4169 | 0 | 4413 |
| Pro long #2 | deepseek-v4-pro | 4169 | 0 | 4294 |
| Flash long #1 | deepseek-v4-flash-0731 | 4169 | 0 | 1870 |
| Flash long #2 | deepseek-v4-flash-0731 | 4169 | 0 | 2364 |

The model labels stayed stable during this probe. However, both models reported zero `prompt_tokens_details.cached_tokens` even with a repeated 4169-token prefix.

## Interpretation

This endpoint is more stable than the zzone sample in model labeling, but it did not demonstrate usable prefix-cache hits. Possible explanations include disabled caching, a higher minimum threshold, a different usage-reporting convention, or a cache layer that is not enabled for these aliases. The probe alone cannot distinguish them.

For comparison, the zzone Pro Chat probe reported 4096 cached tokens out of 4367 on an exact repeat, while this endpoint reported 0 out of 4169 on an exact repeat. Therefore this endpoint is **not currently a better cache-hit replacement** based on direct evidence.

## Recommendation

Do not switch production DeepSeek traffic to this endpoint solely to improve cache hit rate. Keep it as a controlled fallback or run one more operator-confirmed test asking whether prefix caching is enabled and which usage field reports it. If the operator confirms caching is enabled but still reports zero, the zzone route is currently the stronger cache candidate for Pro, despite zzone's route/model-label instability.
