# CoinCoin Daily Report Bot

Generates a daily CoinCoin operations dashboard PNG from admin analytics APIs and can send it to Slock.

## Local Run

```bash
export COINCOIN_ADMIN_TOKEN='...'
python3 workers/coincoin-daily-report-bot/report_bot.py --send
```

Default API base: `https://clawfather.up.railway.app`

Required runtime secrets for deployment:
- `COINCOIN_ADMIN_TOKEN`
- `SLOCK_AGENT_TOKEN`
- `SLOCK_SERVER_URL=https://api.slock.ai`
- `SLOCK_REPORT_CHANNEL=#coincoin数据`

## Railway

Deploy this directory as an independent Railway scheduled worker:

- root directory: `workers/coincoin-daily-report-bot`
- start command: `python3 report_bot.py --send`
- schedule: `0 0 * * *`

Keep it separate from the main FastAPI service so rollback only disables the report worker.

## Schedule

Run once per day at `08:00 Asia/Singapore`.

Railway cron expression:

```text
0 0 * * *
```

Railway cron runs in UTC, so 00:00 UTC equals 08:00 Singapore time.

The report expects `/admin/analytics/overview` to include `positive_balance_users` or `users_with_balance`.
If the field is missing, the PNG deliberately shows `待补` and should not be accepted as the final scheduled version.
