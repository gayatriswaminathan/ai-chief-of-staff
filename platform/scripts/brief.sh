#!/usr/bin/env bash
# Print the morning brief. Usage: ./scripts/brief.sh [actor-email]
ACTOR="${1:-leader@example.com}"
curl -s -X POST http://localhost:8002/chat \
  -H "X-Actor: $ACTOR" -H "X-Principal: principal-001" -H "Content-Type: application/json" \
  -d '{"question":"morning brief"}' | python3 -c "import json,sys; print(json.load(sys.stdin)['answer'])"
