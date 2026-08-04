---
type: release
status: active
owner: platform
audience: [user, developer, operator, agent]
updated: 2026-08-04
canonical_for: release-2026-08-claude-routing-hardening
---

# 2026-08 Claude Routing Hardening

Claude Code requests now continue to the next configured provider channel on 408,
429, transport failures, and every 5xx response, including Cloudflare 524. Streaming
requests retry only before client-visible events have started, so partial output is
never replayed.

Failed attempts with zero token usage remain visible in RequestLog without a charge.
Terminal local connection-pool timeouts are now logged as well, while remaining
separate from provider health and upstream burst alerts.

Administrators can open a provider channel's **Upstream Models** view to compare its
active routes with the complete upstream model catalog. Unsupported route model ids
are listed as warnings; incomplete or probe-only catalogs are marked unavailable and
do not disable routes automatically.

Operators should use `claude-haiku-4-5-20251001` only for the applicable Haiku 4.5
routes. Sonnet and Opus routes keep their own upstream identifiers. See the
[Claude Code upstream runbook](../architecture/claude-code-upstream-runbook.md) for
the retry policy, route order, 86 concurrency behavior, and 404 repair procedure.
