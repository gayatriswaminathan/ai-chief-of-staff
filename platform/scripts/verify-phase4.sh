#!/usr/bin/env bash
# Phase 4: delegated actions — tier enforcement, Go/No-Go, audit, chat skill flow.
set -uo pipefail
cd "$(dirname "$0")/.."
pass=0; fail=0
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1"; fail=$((fail+1)); fi; }
COS=(-H 'X-Actor: cos@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json')
DEL=(-H 'X-Actor: delegate@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json')

check "action-service healthy" "curl -sf http://localhost:8004/health"

# Unauthorized: delegate has NO send_email grant -> 403
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8004/actions/propose "${DEL[@]}" \
  -d '{"type":"send_email","to":"someone@example.com","subject":"hi","body":"test"}')
check "delegate blocked from send_email (got $code)" "[ '$code' = '403' ]"

# T0: delegate CAN draft -> DRAFT status, never executable
resp=$(curl -sf -X POST http://localhost:8004/actions/propose "${DEL[@]}" \
  -d '{"type":"draft_email","to":"someone@example.com","subject":"draft test","body":"draft body"}')
d_aid=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin)['record']['actionId'])" 2>/dev/null)
d_status=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin)['record']['status'])" 2>/dev/null)
check "delegate draft_email -> DRAFT (T0) ($d_aid)" "[ '$d_status' = 'DRAFT' ]"

# T1: cos proposes send_email -> PROPOSED, then No-Go cancels
resp=$(curl -sf -X POST http://localhost:8004/actions/propose "${COS[@]}" \
  -d '{"type":"send_email","to":"partner@example.com","subject":"Q3 recap","body":"Draft recap attached."}')
aid1=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin)['record']['actionId'])" 2>/dev/null)
s1=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin)['record']['status'])" 2>/dev/null)
check "cos send_email -> PROPOSED (T1) ($aid1)" "[ '$s1' = 'PROPOSED' ]"

s2=$(curl -sf -X POST "http://localhost:8004/actions/$aid1/confirm" "${COS[@]}" -d '{"decision":"no_go"}' | \
  python3 -c "import json,sys; print(json.load(sys.stdin)['record']['status'])" 2>/dev/null)
check "No-Go cancels ($aid1 -> $s2)" "[ '$s2' = 'CANCELLED' ]"

# T1 Go path: propose again, confirm go -> EXECUTED
resp=$(curl -sf -X POST http://localhost:8004/actions/propose "${COS[@]}" \
  -d '{"type":"send_email","to":"partner@example.com","subject":"Q3 recap v2","body":"Final recap."}')
aid2=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin)['record']['actionId'])" 2>/dev/null)
s3=$(curl -sf -X POST "http://localhost:8004/actions/$aid2/confirm" "${COS[@]}" -d '{"decision":"go"}' | \
  python3 -c "import json,sys; print(json.load(sys.stdin)['record']['status'])" 2>/dev/null)
check "Go executes ($aid2 -> $s3)" "[ '$s3' = 'EXECUTED' ]"

# Double-confirm is rejected (already EXECUTED)
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://localhost:8004/actions/$aid2/confirm" "${COS[@]}" -d '{"decision":"go"}')
check "double confirm rejected (got $code)" "[ '$code' = '409' ]"

# T2 auto: nudge_owner executes immediately against an open commitment
cid=$(curl -sf "http://localhost:8001/commitments?status=OPEN" "${COS[@]}" | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['commitmentId'] if d else '')" 2>/dev/null)
if [ -n "$cid" ]; then
  resp=$(curl -sf -X POST http://localhost:8004/actions/propose "${COS[@]}" -d "{\"type\":\"nudge_owner\",\"commitment_id\":\"$cid\"}")
  s4=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin)['record']['status'])" 2>/dev/null)
  check "nudge_owner auto-executes (T2) on $cid ($s4)" "[ '$s4' = 'EXECUTED' ]"
else
  echo "SKIP  nudge (no open commitment found)"
fi

# Audit trail in Mongo carries confirmation decisions
check "audit: go/no_go decisions recorded" \
  "docker exec cos-mongo mongosh --quiet --eval 'db.getSiblingDB(\"security_events\").action_service.countDocuments({\"authorization.confirmation\": {\$in: [\"go\",\"no_go\",\"auto\"]}})' | grep -qv '^0$'"

# Graph: action node with PROPOSED-by relationship (allow up to 30s)
found=1
for i in $(seq 1 10); do
  if docker exec cos-neo4j cypher-shell -u neo4j -p coslocal1 \
    "MATCH (:Person {email:'cos@example.com'})-[:PROPOSED]->(a:Action {id:'$aid2'}) RETURN a.id" 2>/dev/null | grep -q "$aid2"; then found=0; break; fi
  sleep 3
done
check "indexer wrote action + proposer to Neo4j" "[ $found -eq 0 ]"

# Chat end-to-end: skill -> card -> confirm
card=$(curl -sf -X POST http://localhost:8002/chat "${COS[@]}" \
  -d '{"question":"send an email to partner@example.com"}' | python3 -c "import json,sys; print(json.load(sys.stdin)['answer'])" 2>/dev/null)
c_aid=$(echo "$card" | grep -oE 'A-[A-Z0-9]{8}' | head -1)
check "chat 'send an email' returns Go/No-Go card ($c_aid)" "[ -n '$c_aid' ]"
check "chat 'confirm ... go' executes" \
  "curl -sf -X POST http://localhost:8002/chat ${COS[0]} 'X-Actor: cos@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json' -d '{\"question\":\"confirm $c_aid go\"}' | grep -q EXECUTED"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
