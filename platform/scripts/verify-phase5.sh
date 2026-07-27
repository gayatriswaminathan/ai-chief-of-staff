#!/usr/bin/env bash
# Phase 5: meeting notes -> LLM extraction -> commitments filed via governed path.
set -uo pipefail
cd "$(dirname "$0")/.."
pass=0; fail=0
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1"; fail=$((fail+1)); fi; }
COS=(-H 'X-Actor: cos@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json')

NEXTWEEK=$(python3 -c "from datetime import date, timedelta; print(date.today() + timedelta(days=7))")

# Meeting with notes containing two explicit commitments and one vague statement
resp=$(curl -sf -X POST http://localhost:8003/meetings "${COS[@]}" \
  -d "{\"title\":\"Vendor review sync\",\"start_at\":\"$(date -u +%Y-%m-%d)T10:00:00+00:00\",\"attendees\":[\"delegate@example.com\",\"leader@example.com\",\"cos@example.com\"]}")
mid=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin)['meetingId'])" 2>/dev/null)
check "meeting created ($mid)" "[ -n '$mid' ]"

NOTES="Attendees discussed the vendor contract. delegate@example.com will send the revised vendor contract to leader@example.com by $NEXTWEEK. cos@example.com committed to booking the renewal call with the vendor before $NEXTWEEK. We should probably think about pricing at some point."
curl -sf -X POST "http://localhost:8003/meetings/$mid/notes" "${COS[@]}" \
  -d "{\"notes\": \"$NOTES\"}" >/dev/null
check "notes attached" "curl -sf 'http://localhost:8003/meetings' -H 'X-Actor: cos@example.com' -H 'X-Principal: principal-001' | grep -q 'revised vendor contract'"

# Extraction (LLM) -> files commitments
ext=$(curl -sf -X POST "http://localhost:8003/meetings/$mid/extract" "${COS[@]}")
n=$(echo "$ext" | python3 -c "import json,sys; print(len(json.load(sys.stdin)['created']))" 2>/dev/null)
check "extraction created commitments (got ${n:-0}, expect 2)" "[ '${n:-0}' = '2' ]"

first=$(echo "$ext" | python3 -c "import json,sys; print(json.load(sys.stdin)['created'][0]['commitmentId'])" 2>/dev/null)

# The extraction itself is on the audit trail
check "extraction recorded as security event" \
  "docker exec cos-mongo mongosh --quiet --eval 'db.getSiblingDB(\"security_events\").meeting_service.findOne({entityId: \"$mid\", action: \"extract_commitments\"}).details.created' | grep -q 'C-'"

# Extracted commitments flow to the graph like any other
found=1
for i in $(seq 1 10); do
  if docker exec cos-neo4j cypher-shell -u neo4j -p coslocal1 \
    "MATCH (c:Commitment {id: '$first'}) RETURN c.id" 2>/dev/null | grep -q "$first"; then found=0; break; fi
  sleep 3
done
check "extracted commitment reached the graph" "[ $found -eq 0 ]"

# And chat can see them
check "chat surfaces extracted commitment" \
  "curl -sf -X POST http://localhost:8002/chat -H 'X-Actor: leader@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json' -d '{\"question\":\"anything I should chase people about?\"}' | grep -q 'vendor contract'"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
