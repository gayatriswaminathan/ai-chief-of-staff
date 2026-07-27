#!/usr/bin/env bash
# Phase 7: decision memory — record, supersede chain, graph, chat query + record-via-chat.
set -uo pipefail
cd "$(dirname "$0")/.."
pass=0; fail=0
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1"; fail=$((fail+1)); fi; }
COS=(-H 'X-Actor: cos@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json')

check "decision-service healthy" "curl -sf http://localhost:8005/health"

# Delegate has no record_decision grant -> 403
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8005/decisions \
  -H 'X-Actor: delegate@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json' \
  -d '{"title":"nope"}')
check "delegate blocked from recording (got $code)" "[ '$code' = '403' ]"

# Record decision v1
d1=$(curl -sf -X POST http://localhost:8005/decisions "${COS[@]}" \
  -d '{"title":"Renew with vendor Acme for 12 months","basis":"20% multi-year discount; migration cost too high","alternatives":["switch to Bravo","6-month renewal"]}' | \
  python3 -c "import json,sys; print(json.load(sys.stdin)['decisionId'])" 2>/dev/null)
check "decision recorded ($d1)" "[ -n '$d1' ]"

# Supersede it
d2=$(curl -sf -X POST http://localhost:8005/decisions "${COS[@]}" \
  -d "{\"title\":\"Switch to vendor Bravo at renewal\",\"basis\":\"Acme raised prices 30% at renewal\",\"supersedes\":\"$d1\"}" | \
  python3 -c "import json,sys; print(json.load(sys.stdin)['decisionId'])" 2>/dev/null)
check "superseding decision recorded ($d2)" "[ -n '$d2' ]"

check "old decision marked REVERSED" \
  "curl -sf 'http://localhost:8005/decisions?status=REVERSED' -H 'X-Actor: cos@example.com' -H 'X-Principal: principal-001' | grep -q '$d1'"

# Graph: supersedes chain + decided-by (allow up to 30s)
found=1
for i in $(seq 1 10); do
  if docker exec cos-neo4j cypher-shell -u neo4j -p coslocal1 \
    "MATCH (n:Decision {id:'$d2'})-[:SUPERSEDES]->(o:Decision {id:'$d1'}) RETURN n.id" 2>/dev/null | grep -q "$d2"; then found=0; break; fi
  sleep 3
done
check "supersedes chain in graph" "[ $found -eq 0 ]"

check "decided-by in graph" \
  "docker exec cos-neo4j cypher-shell -u neo4j -p coslocal1 \"MATCH (:Decision {id:'$d2'})-[:DECIDED_BY]->(p:Person) RETURN p.email\" | grep -q cos@example.com"

# Chat: query decision memory
check "chat answers 'what did we decide about vendor'" \
  "curl -sf -X POST http://localhost:8002/chat -H 'X-Actor: leader@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json' -d '{\"question\":\"what did we decide about the vendor?\"}' | grep -q Bravo"

# Chat: record a decision conversationally
check "chat records a decision" \
  "curl -sf -X POST http://localhost:8002/chat ${COS[0]} 'X-Actor: cos@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json' -d '{\"question\":\"record decision: weekly staff meeting moves to Tuesdays because Monday conflicts with the exec sync\"}' | grep -q 'Recorded D-'"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
