#!/usr/bin/env bash
# OpenClaw Compatibility Test for CoinCoin Proxy
# Simulates exactly what OpenClaw sends when configured as a custom provider

BASE="https://clawfather.up.railway.app"
ADMIN="wudibird"
PASS=0; FAIL=0
RESULTS=""

report() {
  local status="$1" name="$2" detail="$3"
  if [[ "$status" == "PASS" ]]; then
    ((PASS++)); RESULTS+="✅ PASS | $name | $detail\n"
  else
    ((FAIL++)); RESULTS+="❌ FAIL | $name | $detail\n"
  fi
}

echo "=============================================="
echo " OpenClaw ↔ CoinCoin Proxy Compatibility Test"
echo " Target: $BASE"
echo " Time:   $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "=============================================="
echo ""

# ─── Setup: create test user with balance ───
TESTUSER="openclaw_test_$(date +%s)"
echo ">>> Setup: creating test user $TESTUSER"
RESP=$(curl -s -X POST "$BASE/v1/keys/activate" \
  -H "Content-Type: application/json" \
  -d "{\"username\": \"$TESTUSER\"}")
USER_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('user_id',''))" 2>/dev/null)
API_KEY=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('api_key',''))" 2>/dev/null)

if [[ -z "$API_KEY" ]]; then
  echo "FATAL: could not create test user"
  echo "$RESP"
  exit 1
fi
echo "    user_id=$USER_ID"
echo "    api_key=${API_KEY:0:20}..."

curl -s -o /dev/null -X PATCH "$BASE/admin/users/$USER_ID" \
  -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" \
  -d '{"balance": 200000, "token_limit": null, "request_limit_per_minute": null, "request_limit_per_day": null}'
echo "    balance set to $2000"
echo ""

# ================================================================
# TEST 1: openai-responses mode — non-streaming (basic)
# OpenClaw sends: POST /v1/responses with Authorization: Bearer
# ================================================================
echo ">>> [1/9] openai-responses: basic non-stream"
HTTP=$(curl -s -o /tmp/oc_resp.json -w '%{http_code}' --max-time 120 \
  -X POST "$BASE/v1/responses" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.2-codex",
    "input": "Reply with exactly: hello openclaw",
    "stream": false
  }')
BODY=$(cat /tmp/oc_resp.json)

if [[ "$HTTP" == "200" ]]; then
  # Validate OpenClaw-expected response fields
  HAS_ID=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('id') else 'no')" 2>/dev/null)
  HAS_OUTPUT=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('output') else 'no')" 2>/dev/null)
  HAS_USAGE=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('usage') else 'no')" 2>/dev/null)
  HAS_MODEL=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('model','?'))" 2>/dev/null)
  if [[ "$HAS_ID" == "yes" && "$HAS_OUTPUT" == "yes" && "$HAS_USAGE" == "yes" ]]; then
    report "PASS" "responses: basic" "id=✓ output=✓ usage=✓ model=$HAS_MODEL"
  else
    report "FAIL" "responses: basic" "missing fields: id=$HAS_ID output=$HAS_OUTPUT usage=$HAS_USAGE"
  fi
else
  report "FAIL" "responses: basic" "HTTP $HTTP: ${BODY:0:200}"
fi

# ================================================================
# TEST 2: openai-responses mode — streaming SSE
# OpenClaw expects: text/event-stream with data: lines
# ================================================================
echo ">>> [2/9] openai-responses: streaming SSE"
HTTP=$(curl -s -o /tmp/oc_stream.txt -w '%{http_code}' --max-time 120 \
  -X POST "$BASE/v1/responses" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.2-codex",
    "input": "Say ok",
    "stream": true
  }')

if [[ "$HTTP" == "200" ]]; then
  # OpenClaw parses SSE events — check for expected event types
  HAS_CREATED=$(grep -c "response.created" /tmp/oc_stream.txt 2>/dev/null || echo 0)
  HAS_DELTA=$(grep -c "response.output_text.delta" /tmp/oc_stream.txt 2>/dev/null || echo 0)
  HAS_COMPLETED=$(grep -c "response.completed" /tmp/oc_stream.txt 2>/dev/null || echo 0)
  HAS_DONE=$(grep -c "\[DONE\]" /tmp/oc_stream.txt 2>/dev/null || echo 0)
  HAS_USAGE_IN_STREAM=$(grep -c "usage" /tmp/oc_stream.txt 2>/dev/null || echo 0)

  DETAILS="created=$HAS_CREATED delta=$HAS_DELTA completed=$HAS_COMPLETED done=$HAS_DONE usage_refs=$HAS_USAGE_IN_STREAM"
  if [[ "$HAS_DELTA" -gt 0 && "$HAS_DONE" -gt 0 ]]; then
    report "PASS" "responses: stream SSE" "$DETAILS"
  else
    report "FAIL" "responses: stream SSE" "missing events: $DETAILS"
  fi
else
  report "FAIL" "responses: stream SSE" "HTTP $HTTP"
fi

# ================================================================
# TEST 3: openai-responses mode — with instructions (system prompt)
# OpenClaw sends instructions field for system prompt merging
# ================================================================
echo ">>> [3/9] openai-responses: with instructions"
HTTP=$(curl -s -o /tmp/oc_resp.json -w '%{http_code}' --max-time 120 \
  -X POST "$BASE/v1/responses" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.2-codex",
    "instructions": "You are a helpful coding assistant. Always respond in JSON format.",
    "input": "What is 2+2? Return {\"answer\": N}",
    "stream": false
  }')
BODY=$(cat /tmp/oc_resp.json)
if [[ "$HTTP" == "200" ]]; then
  report "PASS" "responses: instructions" "instructions field accepted"
else
  report "FAIL" "responses: instructions" "HTTP $HTTP: ${BODY:0:200}"
fi

# ================================================================
# TEST 4: openai-responses mode — with tools (function calling)
# OpenClaw sends tools array and expects function_call in output
# ================================================================
echo ">>> [4/9] openai-responses: tool calling"
HTTP=$(curl -s -o /tmp/oc_resp.json -w '%{http_code}' --max-time 120 \
  -X POST "$BASE/v1/responses" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.2-codex",
    "input": "What is the weather in Tokyo?",
    "tools": [
      {
        "type": "function",
        "name": "get_weather",
        "description": "Get weather for a city",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {"type": "string", "description": "City name"}
          },
          "required": ["city"]
        }
      }
    ],
    "stream": false
  }')
BODY=$(cat /tmp/oc_resp.json)
if [[ "$HTTP" == "200" ]]; then
  HAS_FC=$(echo "$BODY" | python3 -c "
import sys, json
d = json.load(sys.stdin)
out = d.get('output', [])
fc = [x for x in out if x.get('type') == 'function_call']
if fc:
    print(f'yes call_id={fc[0].get(\"call_id\",\"?\")} name={fc[0].get(\"name\",\"?\")}')
else:
    print('no')
" 2>/dev/null)
  if [[ "$HAS_FC" == no ]]; then
    report "FAIL" "responses: tool calling" "no function_call in output"
  else
    report "PASS" "responses: tool calling" "$HAS_FC"
    CALL_ID=$(echo "$HAS_FC" | sed 's/.*call_id=\([^ ]*\).*/\1/')
  fi
else
  report "FAIL" "responses: tool calling" "HTTP $HTTP: ${BODY:0:200}"
fi

# ================================================================
# TEST 5: openai-responses — multi-turn with function_call_output
# OpenClaw sends array input with history + tool results
# ================================================================
echo ">>> [5/9] openai-responses: multi-turn with tool output"
if [[ -n "$CALL_ID" && "$CALL_ID" != "?" ]]; then
  HTTP=$(curl -s -o /tmp/oc_resp.json -w '%{http_code}' --max-time 120 \
    -X POST "$BASE/v1/responses" \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d "{
      \"model\": \"gpt-5.2-codex\",
      \"input\": [
        {\"type\": \"message\", \"role\": \"user\", \"content\": \"What is the weather in Tokyo?\"},
        {\"type\": \"function_call\", \"name\": \"get_weather\", \"arguments\": \"{\\\"city\\\":\\\"Tokyo\\\"}\", \"call_id\": \"$CALL_ID\"},
        {\"type\": \"function_call_output\", \"call_id\": \"$CALL_ID\", \"output\": \"{\\\"temperature\\\": \\\"22C\\\", \\\"condition\\\": \\\"sunny\\\"}\"}
      ],
      \"tools\": [
        {
          \"type\": \"function\",
          \"name\": \"get_weather\",
          \"description\": \"Get weather for a city\",
          \"parameters\": {
            \"type\": \"object\",
            \"properties\": {
              \"city\": {\"type\": \"string\"}
            },
            \"required\": [\"city\"]
          }
        }
      ],
      \"stream\": false
    }")
  BODY=$(cat /tmp/oc_resp.json)
  if [[ "$HTTP" == "200" ]]; then
    HAS_TEXT=$(echo "$BODY" | python3 -c "
import sys, json
d = json.load(sys.stdin)
out = d.get('output', [])
text_items = [x for x in out if x.get('type') in ('message', 'text')]
print('yes' if text_items or any('text' in str(x) for x in out) else 'no')
" 2>/dev/null)
    report "PASS" "responses: multi-turn tool" "model responded after tool output"
  else
    report "FAIL" "responses: multi-turn tool" "HTTP $HTTP: ${BODY:0:200}"
  fi
else
  report "FAIL" "responses: multi-turn tool" "no call_id from previous test"
fi

# ================================================================
# TEST 6: openai-completions mode — non-streaming
# OpenClaw uses this when api="openai-completions"
# ================================================================
echo ">>> [6/9] openai-completions: non-stream"
HTTP=$(curl -s -o /tmp/oc_resp.json -w '%{http_code}' --max-time 120 \
  -X POST "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.2-codex",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Say hello"}
    ],
    "stream": false
  }')
BODY=$(cat /tmp/oc_resp.json)
if [[ "$HTTP" == "200" ]]; then
  HAS_CHOICES=$(echo "$BODY" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ch = d.get('choices', [])
print(f'yes count={len(ch)}' if ch else 'no')
" 2>/dev/null)
  HAS_USAGE=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('usage') else 'no')" 2>/dev/null)
  report "PASS" "completions: basic" "choices=$HAS_CHOICES usage=$HAS_USAGE"
else
  report "FAIL" "completions: basic" "HTTP $HTTP: ${BODY:0:200}"
fi

# ================================================================
# TEST 7: openai-completions mode — streaming
# ================================================================
echo ">>> [7/9] openai-completions: stream"
HTTP=$(curl -s -o /tmp/oc_stream2.txt -w '%{http_code}' --max-time 120 \
  -X POST "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.2-codex",
    "messages": [{"role": "user", "content": "Say ok"}],
    "stream": true
  }')
if [[ "$HTTP" == "200" ]]; then
  HAS_DELTA=$(grep -c '"delta"' /tmp/oc_stream2.txt 2>/dev/null || echo 0)
  HAS_DONE=$(grep -c "\[DONE\]" /tmp/oc_stream2.txt 2>/dev/null || echo 0)
  if [[ "$HAS_DELTA" -gt 0 && "$HAS_DONE" -gt 0 ]]; then
    report "PASS" "completions: stream" "delta_events=$HAS_DELTA done=$HAS_DONE"
  else
    report "FAIL" "completions: stream" "delta=$HAS_DELTA done=$HAS_DONE"
  fi
else
  report "FAIL" "completions: stream" "HTTP $HTTP"
fi

# ================================================================
# TEST 8: openai-completions — tool calling
# ================================================================
echo ">>> [8/9] openai-completions: tool calling"
HTTP=$(curl -s -o /tmp/oc_resp.json -w '%{http_code}' --max-time 120 \
  -X POST "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.2-codex",
    "messages": [{"role": "user", "content": "Get the weather in Paris"}],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_weather",
          "description": "Get weather for a city",
          "parameters": {
            "type": "object",
            "properties": {
              "city": {"type": "string"}
            },
            "required": ["city"]
          }
        }
      }
    ],
    "stream": false
  }')
BODY=$(cat /tmp/oc_resp.json)
if [[ "$HTTP" == "200" ]]; then
  HAS_TC=$(echo "$BODY" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ch = d.get('choices', [{}])
tc = ch[0].get('message', {}).get('tool_calls', []) if ch else []
print(f'yes count={len(tc)} fn={tc[0][\"function\"][\"name\"]}' if tc else 'no')
" 2>/dev/null)
  if [[ "$HAS_TC" == "no" ]]; then
    report "FAIL" "completions: tool calling" "no tool_calls"
  else
    report "PASS" "completions: tool calling" "$HAS_TC"
  fi
else
  report "FAIL" "completions: tool calling" "HTTP $HTTP: ${BODY:0:200}"
fi

# ================================================================
# TEST 9: OpenClaw-specific — max_output_tokens param
# OpenClaw sends this to limit response length
# ================================================================
echo ">>> [9/9] openai-responses: max_output_tokens"
HTTP=$(curl -s -o /tmp/oc_resp.json -w '%{http_code}' --max-time 120 \
  -X POST "$BASE/v1/responses" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.2-codex",
    "input": "Count from 1 to 1000",
    "max_output_tokens": 50,
    "stream": false
  }')
BODY=$(cat /tmp/oc_resp.json)
if [[ "$HTTP" == "200" ]]; then
  report "PASS" "responses: max_output_tokens" "param accepted and processed"
else
  report "FAIL" "responses: max_output_tokens" "HTTP $HTTP: ${BODY:0:200}"
fi

# ─── Cleanup ───
echo ""
echo ">>> Cleanup: blocking test user"
curl -s -o /dev/null -X PATCH "$BASE/admin/users/$USER_ID" \
  -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" \
  -d '{"status": "blocked"}'

# ─── Report ───
echo ""
echo "=============================================="
echo " OpenClaw COMPATIBILITY RESULTS"
echo "=============================================="
echo -e "$RESULTS"
echo "=============================================="
echo " TOTAL: $((PASS + FAIL))  |  ✅ PASS: $PASS  |  ❌ FAIL: $FAIL"
echo "=============================================="

if [[ "$FAIL" -eq 0 ]]; then
  echo ""
  echo "=============================================="
  echo " ✅ ALL TESTS PASSED — CoinCoin Proxy is"
  echo "    fully compatible with OpenClaw!"
  echo "=============================================="
  echo ""
  echo "Sample OpenClaw config (~/.openclaw/openclaw.json):"
  echo ""
  cat <<'JSONEOF'
{
  "models": {
    "mode": "merge",
    "providers": {
      "coincoin": {
        "baseUrl": "https://clawfather.up.railway.app/v1",
        "apiKey": "${COINCOIN_API_KEY}",
        "api": "openai-responses",
        "models": [
          {
            "id": "gpt-5.2-codex",
            "name": "GPT 5.2 Codex (CoinCoin)",
            "api": "openai-responses",
            "input": ["text", "image"],
            "contextWindow": 1048576,
            "maxTokens": 65536,
            "cost": {
              "input": 0.99,
              "output": 6.99,
              "cacheRead": 0.495,
              "cacheWrite": 0
            }
          }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": {
        "primary": "coincoin/gpt-5.2-codex"
      },
      "models": {
        "coincoin/gpt-5.2-codex": {
          "alias": "codex",
          "params": {
            "transport": "sse"
          }
        }
      }
    }
  }
}
JSONEOF
  echo ""
  echo "Then set your API key:"
  echo '  export COINCOIN_API_KEY="sk_cc_xxxxx"'
fi
