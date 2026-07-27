"""Ingestion: the 2 AM collection run.
Reads every connected source, normalizes to a single item schema with
stable IDs (source pointers the brief must cite — never_drop_sources).

In production this module is replaced by connectors:
  outlook.json  -> Microsoft Graph /me/messages (delegated Mail.Read)
  teams.json    -> Graph /chats + /teams channels (Chat.Read; protected API)
  jira.json     -> Jira REST /search?jql=updated>=-3d
  meetings.json -> transcript store (e.g. Teams recap API)
The rest of the pipeline is source-agnostic by design.
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from audit import log

BASE = os.path.join(os.path.dirname(__file__), "..")

def run():
    items = []
    for fname in ["outlook.json", "teams.json", "jira.json", "meetings.json"]:
        path = os.path.join(BASE, "corpus", fname)
        with open(path) as f:
            batch = json.load(f)
        items.extend(batch)
        log("attache.ingest", "read_source",
            f"{fname}: {len(batch)} items", "L3_autonomous")
    items.sort(key=lambda x: x.get("date") or x.get("updated") or "")
    out = os.path.join(BASE, "store", "items.json")
    with open(out, "w") as f:
        json.dump(items, f, indent=1)
    log("attache.ingest", "normalize",
        f"{len(items)} items normalized to store/items.json", "L3_autonomous")
    print(f"ingested {len(items)} items")

if __name__ == "__main__":
    run()
