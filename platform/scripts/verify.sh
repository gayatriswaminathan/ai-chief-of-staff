#!/usr/bin/env bash
# Smoke-test the phase 1 spine: Mongo txn write -> CDC -> Kafka topic; Neo4j up; OPA decision.
set -uo pipefail
cd "$(dirname "$0")/.."
pass=0; fail=0
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1"; fail=$((fail+1)); fi; }

check "MongoDB replica set rs0"      "docker exec cos-mongo mongosh --quiet --eval 'rs.status().ok' | grep -q 1"
check "Kafka broker"                 "docker exec cos-kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:19092 --list"
check "Kafka Connect REST"           "curl -sf http://localhost:8083/connectors"
check "Neo4j browser"                "curl -sf http://localhost:7474"
check "OPA server"                   "curl -sf http://localhost:8181/health"

# OPA decision: delegate with T1 send_email grant should be allowed
check "OPA allows T1 send_email for cos@example.com" \
  "curl -sf http://localhost:8181/v1/data/cos/authz -d '{\"input\":{\"actor\":\"cos@example.com\",\"principal\":\"principal-001\",\"action\":\"send_email\"}}' -H 'Content-Type: application/json' | grep -q '\"allow\":true'"

# OPA decision: unknown actor should be denied
check "OPA denies unknown actor" \
  "curl -sf http://localhost:8181/v1/data/cos/authz -d '{\"input\":{\"actor\":\"stranger@example.com\",\"principal\":\"principal-001\",\"action\":\"send_email\"}}' -H 'Content-Type: application/json' | grep -q '\"allow\":false'"

# End-to-end CDC: insert a commitment, expect it on the Kafka topic
docker exec cos-mongo mongosh --quiet --eval '
  db = db.getSiblingDB("cos_commitments");
  db.commitments.insertOne({commitmentId: "smoke-"+Date.now(), principalId: "principal-001",
    owner: "delegate@example.com", owedTo: "leader@example.com",
    description: "CDC smoke test", status: "OPEN", dueAt: new Date(), version: 1})' >/dev/null 2>&1
sleep 8
check "CDC: commitment reached Kafka topic 'commitments'" \
  "docker exec cos-kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:19092 --topic commitments --from-beginning --timeout-ms 10000 2>/dev/null | grep -q 'CDC smoke test'"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
