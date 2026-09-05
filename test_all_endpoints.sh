#!/usr/bin/env bash
set -uo pipefail

BASE="https://clawfather.up.railway.app"
ADMIN="wudibird"
WEBHOOK="wudibird"

PASS=0; FAIL=0; SKIP=0
RESULTS=""

report() {
  local status="$1" name="$2" code="$3" detail="$4"
  if [[ "$status" == "PASS" ]]; then
    ((PASS++))
    RESULTS+="✅ PASS | $name | HTTP $code | $detail\n"
  elif [[ "$status" == "FAIL" ]]; then
    ((FAIL++))
    RESULTS+="❌ FAIL | $name | HTTP $code | $detail\n"
  else
    ((SKIP++))
    RESULTS+="⏭️  SKIP | $name | $code | $detail\n"
  fi
}

echo "========================================="
echo " CoinCoin Proxy - Full Endpoint Test"
echo " Target: $BASE"
echo " Time:   $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "========================================="
echo ""

# ─── 1. Health Check ───
echo ">>> [1/30] GET /health"
HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/health")
BODY=$(cat /tmp/cc_resp.json)
if [[ "$HTTP" == "200" ]]; then report "PASS" "GET /health" "$HTTP" "$BODY"
else report "FAIL" "GET /health" "$HTTP" "$BODY"; fi

# ─── 2. Activate Key (create test user) ───
TESTUSER="api_test_$(date +%s)"
echo ">>> [2/30] POST /v1/keys/activate (username=$TESTUSER)"
HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' -X POST "$BASE/v1/keys/activate" \
  -H "Content-Type: application/json" \
  -d "{\"username\": \"$TESTUSER\"}")
BODY=$(cat /tmp/cc_resp.json)
if [[ "$HTTP" == "200" || "$HTTP" == "201" ]]; then
  report "PASS" "POST /v1/keys/activate" "$HTTP" "user=$TESTUSER"
  USER_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('user_id',''))" 2>/dev/null || echo "")
  API_KEY=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('api_key',''))" 2>/dev/null || echo "")
  echo "    user_id=$USER_ID  api_key=${API_KEY:0:20}..."
else
  report "FAIL" "POST /v1/keys/activate" "$HTTP" "$BODY"
  USER_ID=""; API_KEY=""
fi

# ─── 3. Auth Register ───
REG_USER="reg_test_$(date +%s)"
echo ">>> [3/30] POST /v1/auth/register (username=$REG_USER)"
HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' -X POST "$BASE/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d "{\"username\": \"$REG_USER\", \"password\": \"testpass123\"}")
BODY=$(cat /tmp/cc_resp.json)
if [[ "$HTTP" == "200" || "$HTTP" == "201" ]]; then
  report "PASS" "POST /v1/auth/register" "$HTTP" "user=$REG_USER"
  SESSION_KEY=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_key',''))" 2>/dev/null || echo "")
  REG_USER_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('user_id',''))" 2>/dev/null || echo "")
else
  report "FAIL" "POST /v1/auth/register" "$HTTP" "$BODY"
  SESSION_KEY=""; REG_USER_ID=""
fi

# ─── 4. Auth Login ───
echo ">>> [4/30] POST /v1/auth/login"
HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' -X POST "$BASE/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\": \"$REG_USER\", \"password\": \"testpass123\"}")
BODY=$(cat /tmp/cc_resp.json)
if [[ "$HTTP" == "200" ]]; then report "PASS" "POST /v1/auth/login" "$HTTP" "OK"
else report "FAIL" "POST /v1/auth/login" "$HTTP" "$BODY"; fi

# ─── Give test user some balance so API calls don't 402 ───
if [[ -n "$USER_ID" ]]; then
  echo ">>> [prep] Setting balance for test user..."
  curl -s -o /dev/null -X PATCH "$BASE/admin/users/$USER_ID" \
    -H "Authorization: Bearer $ADMIN" \
    -H "Content-Type: application/json" \
    -d '{"balance": 100000, "token_limit": null, "request_limit_per_minute": null, "request_limit_per_day": null}'
fi

# ─── 5. GET /v1/models ───
echo ">>> [5/30] GET /v1/models"
if [[ -n "$API_KEY" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/v1/models" \
    -H "Authorization: Bearer $API_KEY")
  BODY=$(cat /tmp/cc_resp.json)
  if [[ "$HTTP" == "200" ]]; then report "PASS" "GET /v1/models" "$HTTP" "OK"
  else report "FAIL" "GET /v1/models" "$HTTP" "$BODY"; fi
else report "SKIP" "GET /v1/models" "-" "no API key"; fi

# ─── 6. GET /v1/balance ───
echo ">>> [6/30] GET /v1/balance"
if [[ -n "$API_KEY" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/v1/balance" \
    -H "Authorization: Bearer $API_KEY")
  BODY=$(cat /tmp/cc_resp.json)
  if [[ "$HTTP" == "200" ]]; then report "PASS" "GET /v1/balance" "$HTTP" "$BODY"
  else report "FAIL" "GET /v1/balance" "$HTTP" "$BODY"; fi
else report "SKIP" "GET /v1/balance" "-" "no API key"; fi

# ─── 7. GET /v1/usage ───
echo ">>> [7/30] GET /v1/usage"
if [[ -n "$API_KEY" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/v1/usage?limit=5" \
    -H "Authorization: Bearer $API_KEY")
  BODY=$(cat /tmp/cc_resp.json)
  if [[ "$HTTP" == "200" ]]; then report "PASS" "GET /v1/usage" "$HTTP" "total=$(echo $BODY | python3 -c 'import sys,json; print(json.load(sys.stdin).get(\"total\",\"?\"))' 2>/dev/null || echo '?')"
  else report "FAIL" "GET /v1/usage" "$HTTP" "$BODY"; fi
else report "SKIP" "GET /v1/usage" "-" "no API key"; fi

# ─── 8. POST /v1/responses (non-stream, minimal) ───
echo ">>> [8/30] POST /v1/responses (non-stream)"
if [[ -n "$API_KEY" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' --max-time 120 \
    -X POST "$BASE/v1/responses" \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-5.2-codex","input":"Say hi in one word","stream":false}')
  BODY=$(cat /tmp/cc_resp.json)
  if [[ "$HTTP" == "200" ]]; then report "PASS" "POST /v1/responses" "$HTTP" "OK"
  else report "FAIL" "POST /v1/responses" "$HTTP" "${BODY:0:200}"; fi
else report "SKIP" "POST /v1/responses" "-" "no API key"; fi

# ─── 9. POST /v1/responses (stream) ───
echo ">>> [9/30] POST /v1/responses (stream)"
if [[ -n "$API_KEY" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp_stream.txt -w '%{http_code}' --max-time 120 \
    -X POST "$BASE/v1/responses" \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-5.2-codex","input":"Say ok","stream":true}')
  STREAM_HEAD=$(head -c 200 /tmp/cc_resp_stream.txt 2>/dev/null || echo "empty")
  if [[ "$HTTP" == "200" ]]; then report "PASS" "POST /v1/responses (stream)" "$HTTP" "stream received"
  else report "FAIL" "POST /v1/responses (stream)" "$HTTP" "${STREAM_HEAD:0:200}"; fi
else report "SKIP" "POST /v1/responses (stream)" "-" "no API key"; fi

# ─── 10. POST /v1/chat/completions (non-stream) ───
echo ">>> [10/30] POST /v1/chat/completions"
if [[ -n "$API_KEY" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' --max-time 120 \
    -X POST "$BASE/v1/chat/completions" \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-5.2-codex","messages":[{"role":"user","content":"Say hi"}],"stream":false}')
  BODY=$(cat /tmp/cc_resp.json)
  if [[ "$HTTP" == "200" ]]; then report "PASS" "POST /v1/chat/completions" "$HTTP" "OK"
  else report "FAIL" "POST /v1/chat/completions" "$HTTP" "${BODY:0:200}"; fi
else report "SKIP" "POST /v1/chat/completions" "-" "no API key"; fi

# ─── 11. POST /v1/chat/completions (stream) ───
echo ">>> [11/30] POST /v1/chat/completions (stream)"
if [[ -n "$API_KEY" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp_stream2.txt -w '%{http_code}' --max-time 120 \
    -X POST "$BASE/v1/chat/completions" \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-5.2-codex","messages":[{"role":"user","content":"Say ok"}],"stream":true}')
  STREAM_HEAD=$(head -c 200 /tmp/cc_resp_stream2.txt 2>/dev/null || echo "empty")
  if [[ "$HTTP" == "200" ]]; then report "PASS" "POST /v1/chat/completions (stream)" "$HTTP" "stream received"
  else report "FAIL" "POST /v1/chat/completions (stream)" "$HTTP" "${STREAM_HEAD:0:200}"; fi
else report "SKIP" "POST /v1/chat/completions (stream)" "-" "no API key"; fi

# ─── 12. POST /v1/orders/create ───
echo ">>> [12/30] POST /v1/orders/create"
if [[ -n "$API_KEY" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' --max-time 30 \
    -X POST "$BASE/v1/orders/create" \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"money":"9.90","name":"test order","pay_type":"alipay"}')
  BODY=$(cat /tmp/cc_resp.json)
  if [[ "$HTTP" == "200" || "$HTTP" == "201" ]]; then
    report "PASS" "POST /v1/orders/create" "$HTTP" "$(echo $BODY | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("order_no","?"))' 2>/dev/null || echo '?')"
    ORDER_NO=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('order_no',''))" 2>/dev/null || echo "")
  else
    report "FAIL" "POST /v1/orders/create" "$HTTP" "${BODY:0:200}"
    ORDER_NO=""
  fi
else report "SKIP" "POST /v1/orders/create" "-" "no API key"; ORDER_NO=""; fi

# ─── 13. POST /v1/orders/confirm ───
echo ">>> [13/30] POST /v1/orders/confirm"
if [[ -n "$API_KEY" && -n "$ORDER_NO" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' --max-time 30 \
    -X POST "$BASE/v1/orders/confirm" \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"order_no\": \"$ORDER_NO\"}")
  BODY=$(cat /tmp/cc_resp.json)
  # confirm might fail if payment not actually made - that's expected
  if [[ "$HTTP" == "200" || "$HTTP" == "400" || "$HTTP" == "402" || "$HTTP" == "422" ]]; then
    report "PASS" "POST /v1/orders/confirm" "$HTTP" "endpoint reachable: ${BODY:0:100}"
  else report "FAIL" "POST /v1/orders/confirm" "$HTTP" "${BODY:0:200}"; fi
else report "SKIP" "POST /v1/orders/confirm" "-" "no order_no"; fi

# ═══ ADMIN ENDPOINTS ═══

# ─── 14. GET /admin/users ───
echo ">>> [14/30] GET /admin/users"
HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/admin/users" \
  -H "Authorization: Bearer $ADMIN")
BODY=$(cat /tmp/cc_resp.json)
if [[ "$HTTP" == "200" ]]; then
  COUNT=$(echo "$BODY" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
  report "PASS" "GET /admin/users" "$HTTP" "count=$COUNT"
else report "FAIL" "GET /admin/users" "$HTTP" "${BODY:0:200}"; fi

# ─── 15. GET /admin/users?search= ───
echo ">>> [15/30] GET /admin/users?search=$TESTUSER"
HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/admin/users?search=$TESTUSER" \
  -H "Authorization: Bearer $ADMIN")
BODY=$(cat /tmp/cc_resp.json)
if [[ "$HTTP" == "200" ]]; then report "PASS" "GET /admin/users?search=" "$HTTP" "OK"
else report "FAIL" "GET /admin/users?search=" "$HTTP" "${BODY:0:200}"; fi

# ─── 16. GET /admin/users/{id} ───
echo ">>> [16/30] GET /admin/users/{id}"
if [[ -n "$USER_ID" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/admin/users/$USER_ID" \
    -H "Authorization: Bearer $ADMIN")
  BODY=$(cat /tmp/cc_resp.json)
  if [[ "$HTTP" == "200" ]]; then report "PASS" "GET /admin/users/{id}" "$HTTP" "OK"
  else report "FAIL" "GET /admin/users/{id}" "$HTTP" "${BODY:0:200}"; fi
else report "SKIP" "GET /admin/users/{id}" "-" "no user_id"; fi

# ─── 17. PATCH /admin/users/{id} ───
echo ">>> [17/30] PATCH /admin/users/{id}"
if [[ -n "$USER_ID" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' -X PATCH "$BASE/admin/users/$USER_ID" \
    -H "Authorization: Bearer $ADMIN" \
    -H "Content-Type: application/json" \
    -d '{"token_limit": 999999, "request_limit_per_minute": 60, "request_limit_per_day": 5000}')
  BODY=$(cat /tmp/cc_resp.json)
  if [[ "$HTTP" == "200" ]]; then report "PASS" "PATCH /admin/users/{id}" "$HTTP" "OK"
  else report "FAIL" "PATCH /admin/users/{id}" "$HTTP" "${BODY:0:200}"; fi
else report "SKIP" "PATCH /admin/users/{id}" "-" "no user_id"; fi

# ─── 18. POST /admin/users/{id}/reset-usage ───
echo ">>> [18/30] POST /admin/users/{id}/reset-usage"
if [[ -n "$USER_ID" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' -X POST "$BASE/admin/users/$USER_ID/reset-usage" \
    -H "Authorization: Bearer $ADMIN")
  BODY=$(cat /tmp/cc_resp.json)
  if [[ "$HTTP" == "200" ]]; then report "PASS" "POST /admin/users/{id}/reset-usage" "$HTTP" "$BODY"
  else report "FAIL" "POST /admin/users/{id}/reset-usage" "$HTTP" "${BODY:0:200}"; fi
else report "SKIP" "POST /admin/users/{id}/reset-usage" "-" "no user_id"; fi

# ─── 19. POST /admin/users/{id}/keys ───
echo ">>> [19/30] POST /admin/users/{id}/keys"
if [[ -n "$USER_ID" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' -X POST "$BASE/admin/users/$USER_ID/keys" \
    -H "Authorization: Bearer $ADMIN")
  BODY=$(cat /tmp/cc_resp.json)
  if [[ "$HTTP" == "200" || "$HTTP" == "201" ]]; then
    NEW_KEY_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")
    report "PASS" "POST /admin/users/{id}/keys" "$HTTP" "key_id=$NEW_KEY_ID"
  else report "FAIL" "POST /admin/users/{id}/keys" "$HTTP" "${BODY:0:200}"; NEW_KEY_ID=""; fi
else report "SKIP" "POST /admin/users/{id}/keys" "-" "no user_id"; NEW_KEY_ID=""; fi

# ─── 20. GET /admin/keys ───
echo ">>> [20/30] GET /admin/keys"
HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/admin/keys" \
  -H "Authorization: Bearer $ADMIN")
BODY=$(cat /tmp/cc_resp.json)
if [[ "$HTTP" == "200" ]]; then report "PASS" "GET /admin/keys" "$HTTP" "OK"
else report "FAIL" "GET /admin/keys" "$HTTP" "${BODY:0:200}"; fi

# ─── 21. PATCH /admin/keys/{id} ───
echo ">>> [21/30] PATCH /admin/keys/{id}"
if [[ -n "$NEW_KEY_ID" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' -X PATCH "$BASE/admin/keys/$NEW_KEY_ID" \
    -H "Authorization: Bearer $ADMIN" \
    -H "Content-Type: application/json" \
    -d '{"status": "disabled"}')
  BODY=$(cat /tmp/cc_resp.json)
  if [[ "$HTTP" == "200" ]]; then report "PASS" "PATCH /admin/keys/{id}" "$HTTP" "disabled"
  else report "FAIL" "PATCH /admin/keys/{id}" "$HTTP" "${BODY:0:200}"; fi
else report "SKIP" "PATCH /admin/keys/{id}" "-" "no key_id"; fi

# ─── 22. GET /admin/metrics/summary ───
echo ">>> [22/30] GET /admin/metrics/summary"
HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/admin/metrics/summary" \
  -H "Authorization: Bearer $ADMIN")
BODY=$(cat /tmp/cc_resp.json)
if [[ "$HTTP" == "200" ]]; then report "PASS" "GET /admin/metrics/summary" "$HTTP" "$BODY"
else report "FAIL" "GET /admin/metrics/summary" "$HTTP" "${BODY:0:200}"; fi

# ─── 23. GET /admin/usage/daily ───
echo ">>> [23/30] GET /admin/usage/daily"
HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/admin/usage/daily" \
  -H "Authorization: Bearer $ADMIN")
BODY=$(cat /tmp/cc_resp.json)
if [[ "$HTTP" == "200" ]]; then report "PASS" "GET /admin/usage/daily" "$HTTP" "OK"
else report "FAIL" "GET /admin/usage/daily" "$HTTP" "${BODY:0:200}"; fi

# ─── 24. GET /admin/usage/daily?day= ───
echo ">>> [24/30] GET /admin/usage/daily?day=2026-03-06"
HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/admin/usage/daily?day=2026-03-06" \
  -H "Authorization: Bearer $ADMIN")
BODY=$(cat /tmp/cc_resp.json)
if [[ "$HTTP" == "200" ]]; then report "PASS" "GET /admin/usage/daily?day=" "$HTTP" "OK"
else report "FAIL" "GET /admin/usage/daily?day=" "$HTTP" "${BODY:0:200}"; fi

# ─── 25. GET /admin/recharges ───
echo ">>> [25/30] GET /admin/recharges"
HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/admin/recharges" \
  -H "Authorization: Bearer $ADMIN")
BODY=$(cat /tmp/cc_resp.json)
if [[ "$HTTP" == "200" ]]; then report "PASS" "GET /admin/recharges" "$HTTP" "OK"
else report "FAIL" "GET /admin/recharges" "$HTTP" "${BODY:0:200}"; fi

# ─── 26. GET /admin/users/{id}/request-logs ───
echo ">>> [26/30] GET /admin/users/{id}/request-logs"
if [[ -n "$USER_ID" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/admin/users/$USER_ID/request-logs" \
    -H "Authorization: Bearer $ADMIN")
  BODY=$(cat /tmp/cc_resp.json)
  if [[ "$HTTP" == "200" ]]; then report "PASS" "GET /admin/users/{id}/request-logs" "$HTTP" "OK"
  else report "FAIL" "GET /admin/users/{id}/request-logs" "$HTTP" "${BODY:0:200}"; fi
else report "SKIP" "GET /admin/users/{id}/request-logs" "-" "no user_id"; fi

# ─── 27. GET /admin/payment-orders ───
echo ">>> [27/30] GET /admin/payment-orders"
HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/admin/payment-orders" \
  -H "Authorization: Bearer $ADMIN")
BODY=$(cat /tmp/cc_resp.json)
if [[ "$HTTP" == "200" ]]; then report "PASS" "GET /admin/payment-orders" "$HTTP" "OK"
else report "FAIL" "GET /admin/payment-orders" "$HTTP" "${BODY:0:200}"; fi

# ─── 28. GET/POST /admin/announcements ───
echo ">>> [28/30] GET /admin/announcements"
HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/admin/announcements" \
  -H "Authorization: Bearer $ADMIN")
BODY=$(cat /tmp/cc_resp.json)
if [[ "$HTTP" == "200" ]]; then report "PASS" "GET /admin/announcements" "$HTTP" "OK"
else report "FAIL" "GET /admin/announcements" "$HTTP" "${BODY:0:200}"; fi

# ═══ WEBHOOK ENDPOINTS ═══

# ─── 29. POST /webhook/recharge ───
RECHARGE_ORDER="test_order_$(date +%s)"
echo ">>> [29/30] POST /webhook/recharge (order=$RECHARGE_ORDER)"
if [[ -n "$USER_ID" ]]; then
  HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' -X POST "$BASE/webhook/recharge" \
    -H "Authorization: Bearer $WEBHOOK" \
    -H "Content-Type: application/json" \
    -d "{\"order_id\": \"$RECHARGE_ORDER\", \"user_id\": \"$USER_ID\", \"amount\": 100, \"add_balance\": 200, \"note\": \"test recharge\"}")
  BODY=$(cat /tmp/cc_resp.json)
  if [[ "$HTTP" == "200" ]]; then report "PASS" "POST /webhook/recharge" "$HTTP" "$BODY"
  else report "FAIL" "POST /webhook/recharge" "$HTTP" "${BODY:0:200}"; fi
else report "SKIP" "POST /webhook/recharge" "-" "no user_id"; fi

# ─── 30. GET /webhook/recharge/{order_id} ───
echo ">>> [30/30] GET /webhook/recharge/{order_id}"
HTTP=$(curl -s -o /tmp/cc_resp.json -w '%{http_code}' "$BASE/webhook/recharge/$RECHARGE_ORDER" \
  -H "Authorization: Bearer $WEBHOOK")
BODY=$(cat /tmp/cc_resp.json)
if [[ "$HTTP" == "200" ]]; then report "PASS" "GET /webhook/recharge/{order_id}" "$HTTP" "OK"
else report "FAIL" "GET /webhook/recharge/{order_id}" "$HTTP" "${BODY:0:200}"; fi

# ═══ CLEANUP: block the test user ═══
if [[ -n "$USER_ID" ]]; then
  echo ""
  echo ">>> Cleanup: blocking test user $USER_ID"
  curl -s -o /dev/null -X PATCH "$BASE/admin/users/$USER_ID" \
    -H "Authorization: Bearer $ADMIN" \
    -H "Content-Type: application/json" \
    -d '{"status": "blocked"}'
fi
if [[ -n "$REG_USER_ID" ]]; then
  echo ">>> Cleanup: blocking registered test user $REG_USER_ID"
  curl -s -o /dev/null -X PATCH "$BASE/admin/users/$REG_USER_ID" \
    -H "Authorization: Bearer $ADMIN" \
    -H "Content-Type: application/json" \
    -d '{"status": "blocked"}'
fi

# ═══ FINAL REPORT ═══
echo ""
echo "========================================="
echo " TEST RESULTS SUMMARY"
echo "========================================="
echo -e "$RESULTS"
echo "========================================="
echo " TOTAL: $((PASS + FAIL + SKIP))  |  ✅ PASS: $PASS  |  ❌ FAIL: $FAIL  |  ⏭️  SKIP: $SKIP"
echo "========================================="
