# CoinCoin dashboard rolling 24h reporting - Intent

## TaskIntentDraft

- Requested outcome: Admin operating dashboard and daily report use the latest 24 hours for current-period metrics instead of natural-day-to-date data.
- Scope: Update analytics API period semantics, worker fetch/render labels, tests, and deployment verification for CoinCoin daily dashboard.
- Change kinds:
- contract-change
- Risk hints:
- Cross-endpoint analytics contract change; must keep 7d trend and low-balance behavior stable.

## BaselineReadSetHint

- docs/aegis/plans/2026-05-13-operating-dashboard-v2.md

## ImpactStatementDraft

- Compatibility boundary: Existing period values today/7d/30d remain accepted; today changes semantics to rolling 24h and responses include label/window metadata.
- Affected layers:
- admin analytics API, report worker
- Owners:
- app/admin.py and workers/coincoin-daily-report-bot/report_bot.py
- Invariants:
- Current-period dashboard metrics represent the most recent 24 hours; trend remains daily 7d context; no secrets printed.
- Non-goals:
- No redesign of dashboard layout, no new upstream-cost implementation, no channel-type data model change.

These records are Method Pack drafts / hints, not authoritative runtime decisions.
