# Service Reliability Center - Reflection

The work consolidated three overlapping admin surfaces into one route-centric reliability console without moving health logic into request routing. The main compatibility lesson was that retained monitor history changes channel deletion semantics: monitored channels must be disabled rather than hard-deleted to preserve foreign-key-backed evidence.

The active probe engine currently supports text endpoints only. Route reconciliation now skips unsupported image, video, and embedding endpoints instead of generating false failures; those routes remain visible through bounded request-log observation until endpoint-specific probes are implemented deliberately.

Method Pack output does not grant completion authority.
