# CoinCoin Admin Performance Optimization - Reflection

The admin read path now performs fixed-count bulk work for the user page,
combines the three dashboard leaderboards into one request/query, protects
analytics cold builds with single-flight caching, and removes provider
all-history scans from the warm page path. Billing debit, payment, routing,
model selection, and public API behavior were kept outside the change boundary.

Targeted admin and billing regression tests pass. The complete backend suite
still has three video-generation errors that reproduce unchanged on the clean
baseline commit: `public_model_pricing_kwargs` supplies
`effective_cache_creation_input_per_million`, while `RequestLog` does not own
that mapped field. This task does not patch that independent billing-log owner.

Residual risk is bounded to admin reads: the bulk traffic-pack query has a
fixed query count but can return more history rows when a paged user has an
unusually large pack history. The admin UI caps the page at 50 users; production
timing headers and slow logs provide evidence for any follow-up tuning.

Complexity delta: one isolated timing middleware and two cache owners were
added; N+1 list calls, triple leaderboard requests, duplicate cold dashboard
builds, and warm-path historical scans were retired. `app/admin.py` remains an
overloaded owner and should be split by admin domain in a separate refactor,
not during this performance repair.

Method Pack output does not grant completion authority.
