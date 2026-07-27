"""Append-only audit log. Every agent action is recorded — tamper-evident
by hash-chaining each entry to the previous one (pattern: JPMC agent-security
doctrine, 'tamper-evident audit records')."""
import json, hashlib, os
from datetime import datetime, timezone

AUDIT_PATH = os.path.join(os.path.dirname(__file__), "..", "store", "audit.jsonl")

def _last_hash():
    if not os.path.exists(AUDIT_PATH):
        return "GENESIS"
    last = None
    with open(AUDIT_PATH) as f:
        for line in f:
            if line.strip():
                last = line
    return json.loads(last)["hash"] if last else "GENESIS"

def log(actor, action, detail, autonomy_level):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "action": action,
        "detail": detail,
        "autonomy_level": autonomy_level,
        "prev_hash": _last_hash(),
    }
    entry["hash"] = hashlib.sha256(
        json.dumps(entry, sort_keys=True).encode()
    ).hexdigest()[:16]
    with open(AUDIT_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry
