# Upstream `GROUP_DELETED` Error

## Symptom

The Responses client surfaced an upstream 403 body containing `GROUP_DELETED` and
`API Key 所属分组已删除`.

## Root Cause

The final upstream response was passed through the Responses compatibility layer.
The gateway already redacted provider URLs and keys, but treated this provider
configuration failure as an ordinary 403, so the provider's internal error
wording reached the client.

The client-facing URL identifies the CoinCoin endpoint being called; it does not
identify the provider channel. A channel cannot be determined from that message
alone without the matching request log or fallback alert.

## Fix

Classify deleted-group markers as an upstream service-availability failure. The
gateway now returns HTTP 503 with the existing generic service-unavailable
message and code, while internal logging and fallback diagnostics retain the
original status/code for operators.

## Evidence

- Four targeted Responses error-handling tests passed, including the regression
  test for `GROUP_DELETED`.
- Python compilation and whitespace checks passed.

## Status

DONE
