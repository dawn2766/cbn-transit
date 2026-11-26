#!/bin/bash
# smoke-test.sh - verify a deployed relay layer by layer.
#
# Each check isolates one hop, so the first failure tells you exactly where the
# chain is broken. Reads no secrets from disk: pass them in the environment.
#
#   BASE_URL=https://ai.example.com PROXY_KEY=sk-... ./smoke-test.sh
#   BASE_URL=https://ai.example.com PROXY_KEY=sk-... MODEL=deepseek-v4.1-flash ./smoke-test.sh
#
# Exit code 0 = every check passed.
set -uo pipefail

BASE_URL="${BASE_URL:?set BASE_URL, e.g. https://ai.example.com}"
PROXY_KEY="${PROXY_KEY:?set PROXY_KEY (the client key, not REG_TOKEN)}"
MODEL="${MODEL:-}"
TIMEOUT="${TIMEOUT:-30}"

pass=0; fail=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=$((fail+1)); }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
head_() { printf '\n%s\n' "$*"; }

code_of() { curl -s -o /tmp/smoke.$$ -w '%{http_code}' --max-time "$TIMEOUT" "$@"; }

head_ "1. relay alive (no auth, CNB not involved)"
c=$(code_of "$BASE_URL/health")
if [ "$c" = "200" ] && grep -q '"status":"ok"' /tmp/smoke.$$; then
  ok "GET /health -> 200 $(head -c 120 /tmp/smoke.$$)"
else
  bad "GET /health -> $c $(head -c 200 /tmp/smoke.$$)"
fi

head_ "2. relay -> workspace with client auth"
c=$(code_of "$BASE_URL/v1/models" -H "Authorization: Bearer $PROXY_KEY")
if [ "$c" = "200" ] && grep -q '"object"' /tmp/smoke.$$; then
  ok "GET /v1/models -> 200"
  if [ -z "$MODEL" ]; then
    MODEL=$(grep -o '"id":"[^"]*"' /tmp/smoke.$$ | head -1 | cut -d'"' -f4)
    [ -n "$MODEL" ] && warn "no MODEL given; using first advertised id: $MODEL"
  fi
else
  bad "GET /v1/models -> $c $(head -c 200 /tmp/smoke.$$)"
fi

head_ "3. auth is actually enforced"
c=$(code_of "$BASE_URL/v1/models" -H "Authorization: Bearer definitely-not-the-key")
[ "$c" = "401" ] && ok "wrong key -> 401" || bad "wrong key -> $c (expected 401)"

if [ -n "${MODEL:-}" ]; then
  head_ "4. end-to-end completion ($MODEL)"
  payload=$(printf '{"model":"%s","messages":[{"role":"user","content":"reply with exactly: PING-OK"}],"max_tokens":32,"stream":false}' "$MODEL")
  c=$(code_of "$BASE_URL/v1/chat/completions" -H "Authorization: Bearer $PROXY_KEY" \
        -H 'Content-Type: application/json' -d "$payload")
  if [ "$c" = "200" ] && grep -q '"choices"' /tmp/smoke.$$; then
    ok "POST /v1/chat/completions -> 200"
    grep -q '"usage"' /tmp/smoke.$$ && ok "usage present" || warn "no usage block in response"
  else
    bad "POST /v1/chat/completions -> $c $(head -c 300 /tmp/smoke.$$)"
  fi

  head_ "5. streaming is not buffered"
  payload=$(printf '{"model":"%s","messages":[{"role":"user","content":"count to 5"}],"stream":true}' "$MODEL")
  n=$(curl -sN --max-time "$TIMEOUT" "$BASE_URL/v1/chat/completions" \
        -H "Authorization: Bearer $PROXY_KEY" -H 'Content-Type: application/json' \
        -d "$payload" 2>/dev/null | grep -c '^data: ')
  if [ "${n:-0}" -ge 3 ]; then ok "received $n SSE chunks"; else bad "only ${n:-0} SSE chunk(s); check proxy_buffering off"; fi
else
  warn "skipped completion/streaming checks (no model id)"
fi

head_ "6. Anthropic format (optional; only if your client uses it)"
if [ -n "${MODEL:-}" ]; then
  c=$(code_of "$BASE_URL/v1/messages/count_tokens" -H "x-api-key: $PROXY_KEY" \
        -H 'anthropic-version: 2023-06-01' -H 'Content-Type: application/json' \
        -d '{"model":"'"$MODEL"'","messages":[{"role":"user","content":"hi"}]}')
  case "$c" in
    200) ok "POST /v1/messages/count_tokens -> 200" ;;
    401) bad "Anthropic path rejected the key -> 401" ;;
    *)   warn "POST /v1/messages/count_tokens -> $c (may be unsupported upstream)" ;;
  esac
else
  warn "skipped (no model id to send)"
fi

head_ "7. registration endpoint rejects a bad token without 5xx"
# A bad token must be a clean 403 from cnb-register.py. A 502 here means the
# handler crashed (e.g. on a non-ASCII header) instead of answering.
c=$(code_of "$BASE_URL/ops/register" -X POST \
      -H "X-Reg-Token: smoke-test-not-the-token" -H 'Content-Type: application/json' \
      -d '{"uri":"https://aaaa00000000-9001.cnb.run"}')
case "$c" in
  403) ok "POST /ops/register (bad token) -> 403" ;;
  502|504) bad "POST /ops/register -> $c; the register service crashed or is down" ;;
  *)   warn "POST /ops/register (bad token) -> $c (expected 403)" ;;
esac

rm -f /tmp/smoke.$$
printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
