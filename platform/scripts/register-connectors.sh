#!/usr/bin/env bash
# Register the MongoDB CDC source connectors with Kafka Connect (verbose: prints server responses).
set -uo pipefail
cd "$(dirname "$0")/.."

for f in connect/*.json; do
  name=$(python3 -c "import json,sys; print(json.load(open('$f'))['name'])")
  echo "== Registering $name"
  http_code=$(python3 -c "import json; print(json.dumps(json.load(open('$f'))['config']))" | \
    curl -s -o /tmp/connect-resp.json -w "%{http_code}" -X PUT \
      "http://localhost:8083/connectors/$name/config" \
      -H "Content-Type: application/json" \
      --data-binary @-)
  echo "   HTTP $http_code"
  if [ "$http_code" -ge 300 ]; then
    echo "   Response:"; cat /tmp/connect-resp.json; echo
  fi
done

echo
echo "== Registered connectors:"
curl -s http://localhost:8083/connectors; echo
echo
for f in connect/*.json; do
  name=$(python3 -c "import json,sys; print(json.load(open('$f'))['name'])")
  echo "== Status: $name"
  curl -s "http://localhost:8083/connectors/$name/status"; echo
done
