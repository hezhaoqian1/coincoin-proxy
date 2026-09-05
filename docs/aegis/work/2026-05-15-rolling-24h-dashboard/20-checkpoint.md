# CoinCoin dashboard rolling 24h reporting - Checkpoint

- Task ID: slock-29749713-rolling-24h
- Current todo: Push implementation and verify production API after Railway deploy.
- Completed todos:
  - Added RED coverage for overview `today` using `coincoin_request_logs.created_at >= now-24h`.
  - Added revenue-margin cross-day rolling 24h test so previous-day rows inside the 24h window are not dropped.
  - Added top-users `today` coverage using request logs instead of daily aggregates.
  - Updated admin dashboard and report worker labels from natural-day wording to `近24小时`.
- Active slice: deployment verification
- Blocked on: Railway deployment after GitHub push
- Next step: Commit, push, and smoke the production `/admin/analytics/operating-dashboard?period=today` response.
