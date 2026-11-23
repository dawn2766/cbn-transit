#!/bin/bash
# cnb-ai-watchdog.sh - external self-healing watchdog for the CNB AI proxy relay.
#
# Runs from root crontab every minute. Probes the fixed URL
# (FIXED_URL, e.g. https://<your-domain>/health). When unhealthy:
#   1. GET https://api.cnb.cool/workspace/list?slug=<repo> (personal token)
#   2. if a workspace is status=running -> POST /ops/register so nginx re-points
#      the fixed domain at https://<business_id>-<UPSTREAM_PORT>.cnb.run (REG_TOKEN)
#   3. if none is running -> POST /-/workspace/start (10 min cooldown) and poll
#      the fixed URL every 15s for up to 5 min.
#   4. SECOND LEVEL fake-death: if re-registering the running workspace did not
#      help for >= FAKEDEATH_THRESHOLD failed recovery rounds, re-check the
#      unchanged generation and both fixed/direct health before stopping it.
#      Never stop a healthy direct target or a newly running generation.
#      Stop/start uses a 30 min cooldown (state file last_fakedeath).
# Concurrency is guarded with flock; state lives in /var/lib/cnb-ai-watchdog.
#
# Config: /etc/cnb-ai-watchdog.env (chmod 600; see docs/DEPLOY.md)
# Log:    /var/log/cnb-ai-watchdog.log (rotated at 5 MiB -> .1)
#
set -uo pipefail

ENV_FILE="${CNB_AI_WATCHDOG_ENV:-/etc/cnb-ai-watchdog.env}"

# Explicit environment variables win over the env file (lets ops/tests override
# a single knob, e.g. FIXED_URL, without editing the root-only config).
_cfg_vars="FIXED_URL REGISTER_URL CNB_REPO CNB_TOKEN REGISTER_ENV_FILE UPSTREAM_PORT CNB_API WATCHDOG_LOG WATCHDOG_STATE_DIR PROBE_TIMEOUT START_COOLDOWN POLL_INTERVAL POLL_MAX REGISTER_WAIT FAKEDEATH_THRESHOLD FAKEDEATH_COOLDOWN FAKEDEATH_STOP_WAIT"
declare -A _pre=()
for _v in $_cfg_vars; do _pre[$_v]="${!_v:-}"; done
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
for _v in $_cfg_vars; do
  if [ -n "${_pre[$_v]}" ]; then printf -v "$_v" '%s' "${_pre[$_v]}"; fi
done
unset _pre _v

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

LOG="${WATCHDOG_LOG:-/var/log/cnb-ai-watchdog.log}"
STATE_DIR="${WATCHDOG_STATE_DIR:-/var/lib/cnb-ai-watchdog}"
FIXED="${FIXED_URL:-}"
REGISTER_URL="${REGISTER_URL:-}"
REPO="${CNB_REPO:-}"
TOKEN="${CNB_TOKEN:-}"
REGISTER_ENV_FILE="${REGISTER_ENV_FILE:-/root/.cnb/register.env}"
UPSTREAM_PORT="${UPSTREAM_PORT:-9001}"
API="${CNB_API:-https://api.cnb.cool}"

# Fail fast on incomplete configuration. An empty FIXED_URL would otherwise look
# like an unhealthy probe, and an unauthenticated LIST failure must never be
# mistaken for a dead workspace (it may trigger a stop).
log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "${WATCHDOG_LOG:-/var/log/cnb-ai-watchdog.log}"; }
_missing=""
[ -n "$FIXED" ]        || _missing="$_missing FIXED_URL"
[ -n "$REGISTER_URL" ] || _missing="$_missing REGISTER_URL"
[ -n "$REPO" ]         || _missing="$_missing CNB_REPO"
[ -n "$TOKEN" ]        || _missing="$_missing CNB_TOKEN"
if [ -n "$_missing" ]; then
  log "CONFIG: missing required setting(s):$_missing (see env file: $ENV_FILE); exiting without probing"
  exit 78
fi

# Tunables (overridable from the env file)
PROBE_TIMEOUT="${PROBE_TIMEOUT:-10}"       # seconds per fixed-domain probe
START_COOLDOWN="${START_COOLDOWN:-600}"    # 10 min: never hammer workspace/start
POLL_INTERVAL="${POLL_INTERVAL:-15}"       # seconds between recovery polls
POLL_MAX="${POLL_MAX:-300}"                # 5 min recovery budget after a start
REGISTER_WAIT="${REGISTER_WAIT:-90}"       # seconds to wait after a re-register
FAKEDEATH_THRESHOLD="${FAKEDEATH_THRESHOLD:-5}"  # consecutive failures before stop+start
FAKEDEATH_COOLDOWN="${FAKEDEATH_COOLDOWN:-1800}" # 30 min between fake-deaths
FAKEDEATH_STOP_WAIT="${FAKEDEATH_STOP_WAIT:-5}"  # sleep between stop and start

mkdir -p "$STATE_DIR" 2>/dev/null || true
LOCK="$STATE_DIR/watchdog.lock"
FAIL_FILE="$STATE_DIR/fail_count"
LAST_START_FILE="$STATE_DIR/last_start"
LAST_FAKEDEATH_FILE="$STATE_DIR/last_fakedeath"
FAIL_TARGET_FILE="$STATE_DIR/failure_target"
STATE_FILE="$STATE_DIR/state"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
  mv -f "$LOG" "$LOG.1" 2>/dev/null || true
fi
touch "$LOG" 2>/dev/null || true

# One-line, length-capped helper so log lines stay readable.
oneline() { printf '%s' "$1" | tr '\n\r\t' '   ' | head -c "${2:-200}"; }

# probe_fixed: 0 = healthy, 1 = unhealthy. Sets PROBE_CODE / PROBE_BODY.
probe_url() {
  local url="$1" raw code body
  raw=$(curl -s --max-time "$PROBE_TIMEOUT" -w '\n%{http_code}' "$url" 2>/dev/null) || true
  if [ -z "$raw" ]; then
    PROBE_CODE="000"; PROBE_BODY="(no response / timeout)"
    return 1
  fi
  code="${raw##*$'\n'}"
  case "$code" in
    [0-9][0-9][0-9]) ;;
    *) code="000" ;;
  esac
  body="${raw%$'\n'*}"
  PROBE_CODE="$code"
  PROBE_BODY="$body"
  [ "$code" = "200" ] || return 1
  printf '%s' "$body" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except (ValueError, OSError):
    sys.exit(1)
sys.exit(0 if isinstance(doc, dict) and doc.get("status") == "ok" else 1)
'
}

probe_fixed() { probe_url "$FIXED"; }

# api_call <method> <url> [json_body] [timeout]: CNB OpenAPI request with the
# personal token. Sets API_CODE (http status, 000 on transport error) and API_BODY.
api_call() {
  local method="$1" url="$2" data="${3:-}" timeout="${4:-30}" raw
  local args=(-s --max-time "$timeout" -X "$method" "$url"
              -H "Authorization: Bearer $TOKEN"
              -H 'Content-Type: application/json'
              -w '\n%{http_code}')
  [ -n "$data" ] && args+=(-d "$data")
  raw=$(curl "${args[@]}" 2>/dev/null) || true
  if [ -z "$raw" ]; then
    API_CODE="000"; API_BODY="(no response / timeout)"
    return 1
  fi
  API_CODE="${raw##*$'\n'}"
  case "$API_CODE" in [0-9][0-9][0-9]) ;; *) API_CODE="000" ;; esac
  API_BODY="${raw%$'\n'*}"
  return 0
}

# list_running_info: sets LIST_INFO="<sn> <business_id>" for the newest
# status=running workspace from workspace/list (empty on failure).
# Exit: 0 = found, 1 = none running, 2 = API/parse error, 3 = transitioning.
# Runs in the caller shell (not a subshell) so API_CODE stays visible for logs.
LIST_INFO=""
list_running_info() {
  LIST_INFO=""
  api_call GET "$API/workspace/list?slug=$REPO" "" 20 || return 2
  [ "$API_CODE" = "200" ] || return 2
  LIST_INFO=$(printf '%s' "$API_BODY" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(2)
if not isinstance(d, dict):
    sys.exit(2)
items = d.get("list", d.get("data"))
if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
    sys.exit(2)
running = [x for x in items if str(x.get("status", "")).lower() == "running"]
running.sort(key=lambda x: str(x.get("create_time") or ""), reverse=True)
if not running:
    transitional = {"starting", "pending", "creating", "stopping"}
    sys.exit(3 if any(str(x.get("status", "")).lower() in transitional for x in items) else 1)
sn = running[0].get("sn") or ""
bid = running[0].get("business_id") or ""
if not sn or not bid:
    sys.exit(2)
print("%s %s" % (sn, bid))
')
}

read_reg_token() {
  [ -f "$REGISTER_ENV_FILE" ] || return 1
  grep -m1 '^REG_TOKEN=' "$REGISTER_ENV_FILE" 2>/dev/null \
    | cut -d= -f2- | tr -d "\"' \r"
}

# register_upstream <business_id>: point the fixed domain at that workspace.
register_upstream() {
  local bid="$1" uri rt resp
  uri="https://${bid}-${UPSTREAM_PORT}.cnb.run"
  rt=$(read_reg_token) || true
  if [ -z "${rt:-}" ]; then
    log "ERROR: REG_TOKEN missing/unreadable in $REGISTER_ENV_FILE"
    return 1
  fi
  resp=$(curl -s --max-time 45 -X POST "$REGISTER_URL" \
           -H "X-Reg-Token: $rt" -H 'Content-Type: application/json' \
           -d "{\"uri\":\"$uri\"}" 2>/dev/null) || true
  log "REGISTER: uri=$uri resp=$(oneline "$resp" 200)"
  case "$resp" in
    *'"ok": true'*|*'"ok":true'*) return 0 ;;
  esac
  return 1
}

# start_workspace: dispatch workspace/start (only when nothing is running),
# with a 10-minute cooldown so a slow boot is never duplicated.
start_workspace() {
  local now last
  now=$(date +%s)
  last=$(cat "$LAST_START_FILE" 2>/dev/null || echo 0)
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  if [ $((now - last)) -lt "$START_COOLDOWN" ]; then
    log "START: cooling down ($((now - last))s < ${START_COOLDOWN}s since last dispatch), skip"
    return 1
  fi
  api_call POST "$API/$REPO/-/workspace/start" '{"branch":"main"}' 60
  log "START: workspace/start http=$API_CODE resp=$(oneline "$API_BODY" 300)"
  case "$API_BODY" in
    *'"sn"'*)
      printf '%s\n' "$now" > "$LAST_START_FILE"
      return 0
      ;;
  esac
  return 1
}

# fake_death <expected_sn>: SECOND-LEVEL recovery for "workspace alive, proxy
# dead". Stops the running workspace and starts a fresh one. Safety rules:
#   - only ever stops an sn that workspace/list reports status=running right now
#     (re-listed immediately before the stop; never hard-coded);
#   - 30 min cooldown (state file last_fakedeath) to avoid stop/start loops;
#   - every failure is logged and returns non-zero, never retried in a loop.
#   0 = stop+start dispatched, 1 = skipped/aborted (see FAKEDEATH log lines).
fake_death() {
  local sn="${1:-}" now last info fresh_sn fresh_bid
  now=$(date +%s)
  last=$(cat "$LAST_FAKEDEATH_FILE" 2>/dev/null || echo 0)
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  if [ $((now - last)) -lt "$FAKEDEATH_COOLDOWN" ]; then
    log "FAKEDEATH: cooling down ($((now - last))s < ${FAKEDEATH_COOLDOWN}s since last fake-death), skip"
    return 1
  fi
  # Re-read workspace/list right before stopping: never stop a non-running sn.
  list_running_info
  local list_rc=$?
  info="$LIST_INFO"
  if [ "$list_rc" -ne 0 ] || [ -z "$info" ]; then
    log "FAKEDEATH: no running workspace visible right now (list_rc=$list_rc), abort without stop"
    return 1
  fi
  read -r fresh_sn fresh_bid <<< "$info"
  if [ -z "${fresh_sn:-}" ]; then
    log "FAKEDEATH: workspace/list returned an empty sn, abort without stop"
    return 1
  fi
  if [ "$fresh_sn" != "$sn" ]; then
    log "FAKEDEATH: running sn changed ($sn -> $fresh_sn), abort; fresh workspace gets its own recovery window"
    echo 0 > "$FAIL_FILE"
    return 1
  fi
  # Fixed-domain failure can be caused by relay nginx/DNS/registration. It is
  # not evidence that a healthy workspace should be destroyed.
  if probe_fixed; then
    log "FAKEDEATH: fixed domain recovered before stop, abort"
    mark_ok "state=healthy recovery=pre-stop-probe"
    return 1
  fi
  if probe_url "https://${fresh_bid}-${UPSTREAM_PORT}.cnb.run/health"; then
    log "FAKEDEATH: target sn=$sn is healthy directly; relay failure, abort without stop"
    echo 0 > "$FAIL_FILE"
    return 1
  fi
  log "FAKEDEATH: stop sn=$sn business_id=$fresh_bid (fixed domain and exact target health both failed)"
  api_call POST "$API/workspace/stop" "{\"sn\":\"$sn\"}" 30
  log "FAKEDEATH: stop sn=$sn http=$API_CODE resp=$(oneline "$API_BODY" 300)"
  case "$API_CODE" in
    2*) ;;
    *)
      log "WARN: FAKEDEATH stop failed (http=$API_CODE), abort without start"
      return 1
      ;;
  esac
  # Cooldown starts as soon as the stop is accepted.
  printf '%s\n' "$now" > "$LAST_FAKEDEATH_FILE"
  sleep "$FAKEDEATH_STOP_WAIT"
  api_call POST "$API/$REPO/-/workspace/start" '{"branch":"main"}' 60
  log "FAKEDEATH: start http=$API_CODE resp=$(oneline "$API_BODY" 300)"
  case "$API_BODY" in
    *'"sn"'*)
      # Same 10 min guard as the no-running branch: a not-yet-visible fresh
      # workspace must not trigger a duplicate start next minute.
      printf '%s\n' "$now" > "$LAST_START_FILE"
      return 0
      ;;
  esac
  log "WARN: FAKEDEATH start response unexpected; next run falls back to list/start"
  return 1
}

# poll_recover <budget_seconds>: wait for the fixed domain, re-registering the
# current (running) workspace whenever it becomes visible.
poll_recover() {
  local budget="$1" deadline info
  deadline=$(( $(date +%s) + budget ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    sleep "$POLL_INTERVAL"
    if probe_fixed; then return 0; fi
    list_running_info || true
    info="$LIST_INFO"
    if [ -n "$info" ]; then
      local _sn _bid
      read -r _sn _bid <<< "$info"
      if [ -n "${_bid:-}" ]; then register_upstream "$_bid" || true; fi
      sleep 3
      if probe_fixed; then return 0; fi
    fi
  done
  return 1
}

mark_ok() {
  printf 'last_ok=%s %s\n' "$(date -Is)" "$1" > "$STATE_FILE"
  echo 0 > "$FAIL_FILE"
}

# ---------------------------------------------------------------- main ----
# All state reads/writes, including healthy resets and failure increments, are
# serialized. Skipped cron invocations must not count against an active run.
exec 9>"$LOCK" || { log "ERROR: cannot open lock $LOCK"; exit 1; }
if ! flock -n 9; then
  log "SKIP: another watchdog run holds the lock; leaving it alone"
  exit 0
fi

if probe_fixed; then
  fails=$(cat "$FAIL_FILE" 2>/dev/null || echo 0)
  case "$fails" in ''|*[!0-9]*) fails=0 ;; esac
  if [ "$fails" -gt 0 ]; then
    log "OK: fixed domain healthy again (cleared $fails failed probe(s))"
    mark_ok "state=healthy"
  fi
  exit 0
fi

fails=$(cat "$FAIL_FILE" 2>/dev/null || echo 0)
case "$fails" in ''|*[!0-9]*) fails=0 ;; esac
fails=$((fails + 1))
echo "$fails" > "$FAIL_FILE"
log "UNHEALTHY: fixed probe http=$PROBE_CODE body=$(oneline "$PROBE_BODY" 180) fails=$fails"

[ -n "$REPO" ]  || { log "ERROR: CNB_REPO missing in $ENV_FILE"; exit 1; }
[ -n "$TOKEN" ] || { log "ERROR: CNB_TOKEN missing in $ENV_FILE"; exit 1; }

list_running_info
rc=$?
info="$LIST_INFO"
run_sn=""
run_bid=""
case "$rc" in
  0)
    read -r run_sn run_bid <<< "$info"
    log "LIST: newest running sn=$run_sn business_id=$run_bid"
    previous_sn=$(cat "$FAIL_TARGET_FILE" 2>/dev/null || true)
    if [ "$previous_sn" != "$run_sn" ]; then
      # Failure history belongs to one generation. A newly running workspace
      # must receive a full grace period even when the old generation failed.
      fails=1
      echo "$fails" > "$FAIL_FILE"
      printf '%s\n' "$run_sn" > "$FAIL_TARGET_FILE"
    fi
    ;;
  1)
    log "LIST: no running workspace"
    printf '\n' > "$FAIL_TARGET_FILE"
    ;;
  3)
    log "LIST: workspace transition in progress; do not duplicate start"
    exit 0
    ;;
  *)
    log "ERROR: workspace/list failed (API auth/network? http=$API_CODE); will retry next minute"
    exit 1
    ;;
esac

if [ -n "${run_sn:-}" ]; then
  register_upstream "$run_bid" || log "WARN: /ops/register did not report ok"
  deadline=$(( $(date +%s) + REGISTER_WAIT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    sleep 5
    if probe_fixed; then
      log "RECOVERED: fixed domain healthy after re-register (business_id=$run_bid, http=$PROBE_CODE)"
      mark_ok "state=healthy business_id=$run_bid recovery=re-register"
      exit 0
    fi
  done
  log "WARN: still unhealthy ${REGISTER_WAIT}s after re-register (business_id=$run_bid); upstream workspace/proxy may be down"

  # SECOND LEVEL: workspace is running but re-register kept failing -> its
  # in-workspace proxy is likely dead/stuck. Escalate to fake-death once the
  # failure streak is long enough and the 30 min cooldown has elapsed.
  if [ "$fails" -ge "$FAKEDEATH_THRESHOLD" ]; then
    log "FAKEDEATH: escalating (fail_count=$fails >= $FAKEDEATH_THRESHOLD, running sn=$run_sn)"
    if fake_death "$run_sn"; then
      if poll_recover "$POLL_MAX"; then
        log "RECOVERED: fixed domain healthy after fake-death restart (http=$PROBE_CODE)"
        mark_ok "state=healthy recovery=fake-death sn=$run_sn"
        exit 0
      fi
      log "ERROR: not recovered within ${POLL_MAX}s after fake-death restart"
      exit 1
    fi
    log "WARN: fake-death not executed (cooldown/abort, see FAKEDEATH lines above)"
  fi
  exit 1
fi

log "ACTION: no running workspace -> dispatch workspace/start"
if ! start_workspace; then
  log "WARN: workspace/start not dispatched (cooldown or API error); retry next minute"
  exit 1
fi

if poll_recover "$POLL_MAX"; then
  log "RECOVERED: fixed domain healthy after workspace start (http=$PROBE_CODE)"
  mark_ok "state=healthy recovery=workspace-start"
  exit 0
fi

log "ERROR: not recovered within ${POLL_MAX}s after workspace/start dispatch"
exit 1
