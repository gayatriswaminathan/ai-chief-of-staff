#!/usr/bin/env bash
# Phase 3: meeting-service -> CDC -> graph -> chat 'today' + 'prep'; calendar sync if ICS URL set.
set -uo pipefail
cd "$(dirname "$0")/.."
pass=0; fail=0
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1"; fail=$((fail+1)); fi; }

check "meeting-service healthy" "curl -sf http://localhost:8003/health"

# Create a meeting (tomorrow, with the delegate as attendee so prep finds open threads)
TOMORROW=$(python3 -c "from datetime import date, timedelta; print(date.today() + timedelta(days=1))")
resp=$(curl -sf -X POST http://localhost:8003/meetings \
  -H 'X-Actor: cos@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json' \
  -d "{\"title\":\"Board pack review\",\"start_at\":\"${TOMORROW}T15:00:00+00:00\",\"end_at\":\"${TOMORROW}T15:30:00+00:00\",\"attendees\":[\"delegate@example.com\",\"leader@example.com\"],\"location\":\"Room 4\"}")
mid=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin)['meetingId'])" 2>/dev/null)
check "create meeting via API ($mid)" "[ -n '$mid' ]"

check "meeting security event persisted" \
  "docker exec cos-mongo mongosh --quiet --eval 'db.getSiblingDB(\"security_events\").meeting_service.findOne({entityId: \"$mid\"}).authorization.allowBasis' | grep -q delegation"

# CDC + indexer -> Neo4j
found=1
for i in $(seq 1 10); do
  if docker exec cos-neo4j cypher-shell -u neo4j -p coslocal1 \
    "MATCH (m:Meeting {id: '$mid'}) RETURN m.id" 2>/dev/null | grep -q "$mid"; then found=0; break; fi
  sleep 3
done
check "indexer wrote meeting to Neo4j" "[ $found -eq 0 ]"

check "attendees linked in graph" \
  "docker exec cos-neo4j cypher-shell -u neo4j -p coslocal1 \"MATCH (p:Person {email:'delegate@example.com'})-[:ATTENDS]->(m:Meeting {id:'$mid'}) RETURN p.email\" | grep -q delegate"

check "chat 'today' lists the meeting" \
  "curl -sf -X POST http://localhost:8002/chat -H 'X-Actor: leader@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json' -d '{\"question\":\"what does my week look like\"}' | grep -q 'Board pack review'"

# Prep should surface the meeting AND the open commitment owned by the attendee (from phase 2)
check "chat 'prep' builds a brief with open threads" \
  "curl -sf -X POST http://localhost:8002/chat -H 'X-Actor: leader@example.com' -H 'X-Principal: principal-001' -H 'Content-Type: application/json' -d '{\"question\":\"prep me for board pack\"}' | grep -q 'Open threads'"

# Calendar sync (only if a real ICS URL is configured)
if grep -qs "CALENDAR_ICS_URL=." .env 2>/dev/null; then
  synced=$(docker logs cos-calendar-connector 2>&1 | grep -c "^sync:")
  check "calendar-connector has synced at least once" "[ '$synced' -ge 1 ]"
  ics_count=$(curl -sf "http://localhost:8003/meetings" -H 'X-Actor: leader@example.com' -H 'X-Principal: principal-001' | python3 -c "import json,sys; print(sum(1 for m in json.load(sys.stdin) if m.get('source')=='ics'))" 2>/dev/null)
  check "real calendar events ingested (found ${ics_count:-0})" "[ '${ics_count:-0}' -ge 1 ]"
else
  echo "SKIP  calendar sync (no CALENDAR_ICS_URL in .env)"
fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
