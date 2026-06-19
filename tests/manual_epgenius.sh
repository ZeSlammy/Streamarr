#!/usr/bin/env bash
# Manual integration tests for the EPGenius endpoints.
# Run from DellyFin after `docker compose up -d --build`.
#
# Usage:
#   bash tests/manual_epgenius.sh
#   bash tests/manual_epgenius.sh http://192.168.1.26:8011   # override base URL
# ---------------------------------------------------------------------------

BASE=${1:-http://192.168.1.26:8011}
PASS=0
FAIL=0

_ok()   { echo "  [PASS] $1"; ((PASS++)); }
_fail() { echo "  [FAIL] $1"; ((FAIL++)); }
_sep()  { echo; echo "--- $1 ---"; }

# ---------------------------------------------------------------------------
# 1. Manual push — triggers push_credentials() with the live active URL
# ---------------------------------------------------------------------------
_sep "POST /providers/epgenius-sync (manual push)"
BODY=$(curl -s -X POST "$BASE/providers/epgenius-sync")
echo "  Response: $BODY"

if echo "$BODY" | grep -qi "updated\|success"; then
    _ok "Response contains success indicator"
elif echo "$BODY" | grep -qi "not configured\|disabled"; then
    _ok "Correctly reports EPGenius not configured (check .env vars)"
elif echo "$BODY" | grep -qi "No active provider"; then
    _fail "No active URL in URL.md — add a CURRENT: entry first"
else
    _fail "Unexpected response: $BODY"
fi

# ---------------------------------------------------------------------------
# 2. Provider check + failover (end-to-end, only meaningful if current URL
#    is dead and a candidate exists — otherwise just validates the endpoint)
# ---------------------------------------------------------------------------
_sep "POST /providers/check (failover probe)"
BODY=$(curl -s -X POST "$BASE/providers/check")
echo "  Response: $BODY"
if echo "$BODY" | grep -qi "Probing\|running\|already"; then
    _ok "Provider check triggered"
else
    _fail "Unexpected response from /providers/check"
fi

# Give the background task time to complete
sleep 3

_sep "GET /providers/status (verify last summary)"
STATUS=$(curl -s "$BASE/providers/status")
echo "  Response length: ${#STATUS} chars"
if [ ${#STATUS} -gt 50 ]; then
    _ok "Status page returned content"
else
    _fail "Status page returned too little content"
fi

# ---------------------------------------------------------------------------
# 3. M3U verify — fetch the Google Drive playlist and check the base URL
# ---------------------------------------------------------------------------
_sep "GET /providers/epgenius-verify (M3U check)"
BODY=$(curl -s "$BASE/providers/epgenius-verify")
echo "  Response: $BODY"
if echo "$BODY" | grep -qi "confirmed\|matches"; then
    _ok "M3U base URL matches active provider"
elif echo "$BODY" | grep -qi "Mismatch\|still points"; then
    _fail "M3U base URL does not match active provider (EPGenius may not have rebuilt yet)"
elif echo "$BODY" | grep -qi "not configured"; then
    _ok "EPGENIUS_M3U_URL not set — skipped (add it to .env to enable this check)"
else
    _fail "Unexpected response: $BODY"
fi

# ---------------------------------------------------------------------------
# 4. Direct EPGenius API call (bypasses xtream-picker — confirms your creds
#    work against the EPGenius endpoint directly)
#    Requires: API_KEY, DISCORD_ID, PLAYLIST_ID, DNS set below or in env.
# ---------------------------------------------------------------------------
_sep "Direct EPGenius API call (requires your real creds)"

API_KEY=${EPGENIUS_API_KEY:-}
DISCORD_ID=${EPGENIUS_DISCORD_ID:-}
PLAYLIST_ID=${EPGENIUS_PLAYLIST_ID:-}
DNS=${EPGENIUS_TEST_DNS:-}      # set to current XTREAM base URL for a real test

if [[ -z "$API_KEY" || -z "$DISCORD_ID" || -z "$PLAYLIST_ID" || -z "$DNS" ]]; then
    echo "  SKIP — export EPGENIUS_API_KEY, EPGENIUS_DISCORD_ID, EPGENIUS_PLAYLIST_ID,"
    echo "         and EPGENIUS_TEST_DNS to run this test."
else
    HTTP_STATUS=$(curl -s -o /tmp/epgenius_direct_response.txt -w "%{http_code}" \
        -X POST https://epgenius.org/api/public/update_creds \
        -H "Authorization: $API_KEY" \
        -H "X-Discord-ID: $DISCORD_ID" \
        -H "Content-Type: application/json" \
        -d "{\"playlist_id\": $PLAYLIST_ID, \"dns\": \"$DNS\", \"username\": \"$XTREAM_USERNAME\", \"password\": \"$XTREAM_PASSWORD\"}")
    BODY=$(cat /tmp/epgenius_direct_response.txt)
    echo "  HTTP $HTTP_STATUS — $BODY"
    if [[ "$HTTP_STATUS" == "200" ]]; then
        _ok "Direct EPGenius API call succeeded"
    else
        _fail "Direct EPGenius API call returned HTTP $HTTP_STATUS"
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
