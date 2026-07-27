#!/usr/bin/env bash
# Phase 2 end-to-end: API (OPA preflight + txn) -> CDC -> Kafka -> indexer -> Neo4j -> chat.
set -uo pipefail
pass=0; fail=0
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1"; fail=$((fail+1)); fi; }

H_COS=(-H "X-Actor: cos@example.com" -H "X-Principal: principal-001")
H_STRANGER=(-H "X-Actor: stranger@example.com" -H "X-Principal: principal-001")

check "commitment-service healthy" "curl -sf http://localhost:8001/health"
check "cos-chat healthy"           "curl -sf http://localhost:8002/health"

# Denied actor cannot create (OPA preflight)
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8001/commitments \
  "${H_STRANGER[@]}" -H "Content-Type: application/json" \
  -d '{"owner":"x@example.com","owed_to":"y@example.com","description":"nope","due_at":"2026-08-01"}')
check "OPA preflight blocks unauthorized create (got $code)" "[ '$code' = '403' ]"

# Authorized create
resp=$(curl -sf -X POST http://localhost:8001/commitments \
  "${H_COS[@]}" -H "Content-Type: application/json" \
  -d '{"owner":"delegate@example.com","owed_to":"leader@example.com","description":"Send board pack draft","due_at":"2026-07-30","project":"board-meeting"}')
cid=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin)['commitmentId'])" 2>/dev/null)
check "create commitment via API ($cid)" "[ -n '$cid' ]"

# Security event written in same txn
check "security event persisted with allow_basis" \
  "docker exec cos-mongo mongosh --quiet --eval 'db.getSiblingDB(\"security_events\").commitment_service.findOne({entityId: \"$cid\"}).authorization.allowBasis' | grep -q delegation"

# CDC + indexer -> Neo4j (allow up to 30s)
found=1
for i in $(seq 1 10); do
  if docker exec cos-neo4j cypher-shell -u neo4j -p coslocal1 \
    "MATCH (c:Commitment {id: '$cid'}) RETURN c.id" 2>/dev/null | grep -q "$cid"; then found=0; break; fi
  sleep 3
done
check "indexer wrote commitment to Neo4j graph" "[ $found -eq 0 ]"

check "audit event in graph with authority basis" \
  "docker exec cos-neo4j cypher-shell -u neo4j -p coslocal1 \"MATCH (:Person)-[:PERFORMED]->(e:Event)-[:ON]->(c:Commitment {id: '$cid'}) RETURN e.allowBasis\" | grep -q delegation"

# Chat: me path (as the owner)
check "chat 'me' lists the commitment" \
  "curl -sf -X POST http://localhost:8002/chat -H 'X-Actor: delegate@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json' -d '{\"question\":\"what is on my plate\"}' | grep -q '$cid'"

# Chat: audit path
check "chat 'audit' shows who created it" \
  "curl -sf -X POST http://localhost:8002/chat -H 'X-Actor: cos@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json' -d '{\"question\":\"who created $cid\"}' | grep -q 'create by cos@example.com'"

# Chat: eligibility path (live OPA)
check "chat 'eligibility' answers who CAN send email" \
  "curl -sf -X POST http://localhost:8002/chat -H 'X-Actor: cos@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json' -d '{\"question\":\"who can send email\"}' | grep -q 'cos@example.com'"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
