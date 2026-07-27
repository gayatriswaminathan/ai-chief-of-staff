"""decision-service — the principal's decision memory.

A Decision records: what was decided, by whom, on what basis, what was
considered, and its lifecycle (DECIDED -> REVISITED -> REVERSED or re-DECIDED).
Reversing/superseding never deletes — the chain is preserved for "why did we
change course?" questions.

Same governed write path as every domain service: OPA preflight -> record +
security event in ONE Mongo transaction; CDC ships both.
"""

import os
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://mongo:27017/?replicaSet=rs0")
OPA_URL = os.environ.get("OPA_URL", "http://opa:8181")
SERVICE_ACCOUNT = "svc-decision"

app = FastAPI(title="decision-service")
client = MongoClient(MONGO_URI)
decisions = client["cos_decisions"]["decisions"]
security_events = client["security_events"]["decision_service"]


class DecisionIn(BaseModel):
    title: str = Field(description="what was decided, one line")
    basis: str | None = Field(default=None, description="why — the rationale")
    alternatives: list[str] = Field(default_factory=list, description="options considered")
    decided_by: str | None = Field(default=None, description="defaults to the actor")
    project: str | None = None
    meeting_id: str | None = None
    supersedes: str | None = Field(default=None, description="decision id this replaces")


class RevisitIn(BaseModel):
    reason: str


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
    with client.start_session() as s:
        with s.start_transaction():
            decisions.insert_one(dict(record), session=s)
            security_events.insert_one(dict(event), session=s)


def security_event(actor, principal, action, authz, entity_id, version, details) -> dict:
    return {"eventId": str(uuid.uuid4()), "principalId": principal, "actor": actor,
            "onBehalfOf": SERVICE_ACCOUNT, "action": action, "entityType": "decision",
            "entityId": entity_id, "entityVersion": version, "occurredAt": now(),
            "authorization": authz, "details": details}


def latest(principal: str, did: str):
    return decisions.find_one({"decisionId": did, "principalId": principal},
                              sort=[("version", -1)])


@app.post("/decisions", status_code=201)
def record(body: DecisionIn,
           x_actor: str = Header(...), x_principal: str = Header(...)):
    authz = authorize(x_actor, x_principal, "record_decision")
    if body.supersedes and latest(x_principal, body.supersedes) is None:
        raise HTTPException(404, f"superseded decision {body.supersedes} not found")
    did = f"D-{uuid.uuid4().hex[:8].upper()}"
    record_ = {"decisionId": did, "principalId": x_principal, "title": body.title,
               "basis": body.basis, "alternatives": body.alternatives,
               "decidedBy": body.decided_by or x_actor, "project": body.project,
               "meetingId": body.meeting_id, "supersedes": body.supersedes,
               "status": "DECIDED", "decidedAt": now(),
               "version": 1, "createdAt": now(), "updatedAt": now()}
    event = security_event(x_actor, x_principal, "record", authz, did, 1,
                           {"title": body.title, "supersedes": body.supersedes})
    write_txn(record_, event)

    # mark the superseded decision REVERSED (its own version + event)
    if body.supersedes:
        old = latest(x_principal, body.supersedes)
        v = old["version"] + 1
        old_rec = {**old, "status": "REVERSED", "reversedBy": did,
                   "version": v, "updatedAt": now()}
        old_rec.pop("_id", None)
        ev = security_event(x_actor, x_principal, "reverse", authz,
                            body.supersedes, v, {"reversedBy": did})
        write_txn(old_rec, ev)
    return record_


@app.post("/decisions/{did}/revisit")
def revisit(did: str, body: RevisitIn,
            x_actor: str = Header(...), x_principal: str = Header(...)):
    authz = authorize(x_actor, x_principal, "record_decision")
    current = latest(x_principal, did)
    if current is None:
        raise HTTPException(404, f"decision {did} not found")
    if current["status"] not in ("DECIDED", "REVISITED"):
        raise HTTPException(409, f"{did} is {current['status']}")
    version = current["version"] + 1
    record_ = {**current, "status": "REVISITED", "revisitReason": body.reason,
               "version": version, "updatedAt": now()}
    record_.pop("_id", None)
    event = security_event(x_actor, x_principal, "revisit", authz, did, version,
                           {"reason": body.reason})
    write_txn(record_, event)
    return record_


@app.get("/decisions")
def list_decisions(q: str | None = None, status: str | None = None,
                   x_actor: str = Header(...), x_principal: str = Header(...)):
    authorize(x_actor, x_principal, "record_decision")
    match: dict = {"principalId": x_principal}
    if status:
        match["status"] = status.upper()
    pipeline = [
        {"$match": match},
        {"$sort": {"version": -1}},
        {"$group": {"_id": "$decisionId", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$project": {"_id": 0}},
        {"$sort": {"decidedAt": -1}},
    ]
    out = list(decisions.aggregate(pipeline))
    if q:
        ql = q.lower()
        out = [d for d in out
               if ql in (d.get("title") or "").lower()
               or ql in (d.get("basis") or "").lower()]
    return out


@app.get("/health")
def health():
    return {"ok": True}
