#!/usr/bin/env bash
# Phase 6: daily brief — sections assembled from the graph, routed by chat.
set -uo pipefail
cd "$(dirname "$0")/.."
pass=0; fail=0
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1"; fail=$((fail+1)); fi; }

# Seed one PROPOSED action so the Go/No-Go section has content
curl -sf -X POST http://localhost:8004/actions/propose \
  -H 'X-Actor: cos@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json' \
  -d '{"type":"send_email","to":"brief-test@example.com","subject":"brief section test","body":"x"}' >/dev/null
sleep 6  # let CDC/indexer catch up

brief=$(./scripts/brief.sh)
check "brief renders with header"        "printf '%s' \"\$brief\" | grep -q 'MORNING BRIEF'"
check "brief has meetings section"       "printf '%s' \"\$brief\" | grep -q \"Today's meetings\""
check "brief flags open risk items"      "printf '%s' \"\$brief\" | grep -Eq 'OVERDUE|Due in the next|Slipped'"
check "brief lists pending Go/No-Go"     "printf '%s' \"\$brief\" | grep -q 'Awaiting your Go/No-Go'"
check "LLM routing chose brief path" \
  "curl -sf -X POST http://localhost:8002/chat -H 'X-Actor: leader@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json' -d '{\"question\":\"what do I need to know this morning?\"}' | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d[\"routing\"][\"path\"]==\"brief\", d[\"routing\"][\"path\"]'"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
