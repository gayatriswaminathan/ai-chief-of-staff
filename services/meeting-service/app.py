"""meeting-service — meetings enter the system here (manual or calendar connector).

Same governed write path as every domain service:
  identity -> OPA preflight -> record + security event in ONE Mongo transaction.
Kafka Connect CDC publishes both; this service never touches Kafka.
"""

import os
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from pymongo import MongoClient

import json as jsonlib

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://mongo:27017/?replicaSet=rs0")
OPA_URL = os.environ.get("OPA_URL", "http://opa:8181")
COMMITMENT_URL = os.environ.get("COMMITMENT_SERVICE_URL", "http://commitment-service:8000")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SERVICE_ACCOUNT = "svc-meeting"

app = FastAPI(title="meeting-service")
client = MongoClient(MONGO_URI)
meetings = client["cos_meetings"]["meetings"]
security_events = client["security_events"]["meeting_service"]


class MeetingIn(BaseModel):
    title: str
    start_at: str = Field(description="ISO datetime")
    end_at: str | None = None
    attendees: list[str] = Field(default_factory=list, description="emails")
    location: str | None = None
    project: str | None = None
    source: str = "manual"
    ics_uid: str | None = None


class NotesIn(BaseModel):
    notes: str


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def authorize(actor: str, principal: str, action: str) -> dict:
    r = httpx.post(f"{OPA_URL}/v1/data/cos/authz",
                   json={"input": {"actor": actor, "principal": principal, "action": action}},
                   timeout=5)
    r.raise_for_status()
    d = r.json().get("result", {})
    if not d.get("allow"):
        raise HTTPException(403, f"OPA denied '{action}' for {actor} on {principal}")
    return {"allow": True, "allowBasis": d.get("allow_basis", "unspecified"),
            "tier": d.get("tier", "none")}


def write_txn(record: dict, event: dict) -> None:
    # insert copies: pymongo mutates the inserted dict (adds ObjectId _id)
    with client.start_session() as s:
        with s.start_transaction():
            meetings.insert_one(dict(record), session=s)
            security_events.insert_one(dict(event), session=s)


def security_event(actor, principal, action, authz, entity_id, version, details) -> dict:
    return {"eventId": str(uuid.uuid4()), "principalId": principal, "actor": actor,
            "onBehalfOf": SERVICE_ACCOUNT, "action": action, "entityType": "meeting",
            "entityId": entity_id, "entityVersion": version, "occurredAt": now(),
            "authorization": authz, "details": details}


def latest(query: dict):
    return meetings.find_one(query, sort=[("version", -1)])


@app.post("/meetings", status_code=201)
def ingest(body: MeetingIn,
           x_actor: str = Header(...), x_principal: str = Header(...)):
    authz = authorize(x_actor, x_principal, "ingest_meeting")

    # Idempotent for calendar sync: same ics_uid + start -> update only if changed.
    if body.ics_uid:
        existing = latest({"principalId": x_principal, "icsUid": body.ics_uid,
                           "startAt": body.start_at})
        if existing:
            unchanged = (existing.get("title") == body.title
                         and existing.get("endAt") == body.end_at
                         and existing.get("attendees") == body.attendees
                         and existing.get("location") == body.location)
            if unchanged:
                existing.pop("_id", None)
                return existing
            version = existing["version"] + 1
            record = {**existing, "title": body.title, "endAt": body.end_at,
                      "attendees": body.attendees, "location": body.location,
                      "version": version, "updatedAt": now()}
            record.pop("_id", None)
            event = security_event(x_actor, x_principal, "update", authz,
                                   existing["meetingId"], version, {"source": body.source})
            write_txn(record, event)
            return record

    mid = f"M-{uuid.uuid4().hex[:8].upper()}"
    record = {"meetingId": mid, "principalId": x_principal, "title": body.title,
              "startAt": body.start_at, "endAt": body.end_at, "attendees": body.attendees,
              "location": body.location, "project": body.project, "source": body.source,
              "icsUid": body.ics_uid, "notes": None, "status": "SCHEDULED",
              "version": 1, "createdAt": now(), "updatedAt": now()}
    event = security_event(x_actor, x_principal, "create", authz, mid, 1,
                           {"title": body.title, "source": body.source})
    write_txn(record, event)
    record.pop("_id", None)
    return record


@app.post("/meetings/{mid}/notes")
def attach_notes(mid: str, body: NotesIn,
                 x_actor: str = Header(...), x_principal: str = Header(...)):
    authz = authorize(x_actor, x_principal, "ingest_meeting")
    current = latest({"meetingId": mid, "principalId": x_principal})
    if current is None:
        raise HTTPException(404, f"meeting {mid} not found")
    version = current["version"] + 1
    record = {**current, "notes": body.notes, "version": version, "updatedAt": now()}
    record.pop("_id", None)
    event = security_event(x_actor, x_principal, "attach_notes", authz, mid, version,
                           {"notesChars": len(body.notes)})
    write_txn(record, event)
    return record


EXTRACT_SYSTEM = """You extract commitments from meeting notes for a Chief of Staff system.
A commitment is an explicit promise: someone will deliver something, ideally by a date.

Return ONLY a JSON array (no prose). Each element:
{"owner_email": "...", "owed_to_email": "...", "description": "...", "due_at": "YYYY-MM-DD" or null}

Rules:
- Only explicit commitments ("X will send Y by Friday"). No vague intentions.
- Map names to the attendee emails provided. If an owner can't be mapped to an email, skip it.
- If who it's owed to is unclear, use the principal's email (provided).
- Resolve relative dates ("Friday", "next week") against the meeting date provided.
- Empty array if none. Never invent."""


@app.post("/meetings/{mid}/extract")
def extract(mid: str,
            x_actor: str = Header(...), x_principal: str = Header(...)):
    """LLM-extract commitments from the meeting's notes and file each one through
    commitment-service — the governed path (OPA preflight, txn, CDC, audit)."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(503, "extraction requires ANTHROPIC_API_KEY")
    authz = authorize(x_actor, x_principal, "ingest_meeting")
    m = latest({"meetingId": mid, "principalId": x_principal})
    if m is None:
        raise HTTPException(404, f"meeting {mid} not found")
    if not m.get("notes"):
        raise HTTPException(409, f"meeting {mid} has no notes attached")

    principal_email = "leader@example.com"  # dev default; from ZITADEL later
    prompt = (f"Meeting: {m['title']}  date: {m['startAt'][:10]}\n"
              f"Attendees: {', '.join(m.get('attendees') or [])}\n"
              f"Principal: {principal_email}\n\nNOTES:\n{m['notes']}")
    r = httpx.post("https://api.anthropic.com/v1/messages",
                   headers={"x-api-key": ANTHROPIC_API_KEY,
                            "anthropic-version": "2023-06-01"},
                   json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1500,
                         "system": EXTRACT_SYSTEM,
                         "messages": [{"role": "user", "content": prompt}]},
                   timeout=45)
    if r.status_code != 200:
        raise HTTPException(502, f"LLM error: {r.text[:200]}")
    text = r.json()["content"][0]["text"]
    try:
        items = jsonlib.loads(text[text.index("["): text.rindex("]") + 1])
    except (ValueError, jsonlib.JSONDecodeError):
        raise HTTPException(502, f"LLM returned unparseable output: {text[:200]}")

    created, skipped = [], []
    for it in items:
        if not it.get("owner_email") or not it.get("description"):
            skipped.append(it)
            continue
        resp = httpx.post(f"{COMMITMENT_URL}/commitments",
                          json={"owner": it["owner_email"],
                                "owed_to": it.get("owed_to_email") or principal_email,
                                "description": it["description"],
                                "due_at": it.get("due_at") or "unspecified",
                                "project": m.get("project")},
                          headers={"X-Actor": x_actor, "X-Principal": x_principal},
                          timeout=10)
        if resp.status_code == 201:
            created.append(resp.json())
        else:
            skipped.append({**it, "error": resp.text[:100]})

    # record the extraction itself on the audit trail
    version = m["version"] + 1
    record = {**m, "extractedCount": len(created), "version": version, "updatedAt": now()}
    record.pop("_id", None)
    event = security_event(x_actor, x_principal, "extract_commitments", authz, mid, version,
                           {"created": [c["commitmentId"] for c in created],
                            "skipped": len(skipped)})
    write_txn(record, event)
    return {"meetingId": mid, "created": created, "skipped": skipped}


@app.get("/meetings")
def list_meetings(after: str | None = None,
                  x_actor: str = Header(...), x_principal: str = Header(...)):
    authorize(x_actor, x_principal, "ingest_meeting")
    match: dict = {"principalId": x_principal}
    if after:
        match["startAt"] = {"$gte": after}
    pipeline = [
        {"$match": match},
        {"$sort": {"version": -1}},
        {"$group": {"_id": "$meetingId", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$project": {"_id": 0}},
        {"$sort": {"startAt": 1}},
    ]
    return list(meetings.aggregate(pipeline))


@app.get("/health")
def health():
    return {"ok": True}
