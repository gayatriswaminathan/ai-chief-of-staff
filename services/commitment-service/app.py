"""commitment-service — first domain service of the AI Chief of Staff.

Pattern (per docs/architecture-spec.md):
  identity -> OPA preflight (allow + allow_basis) -> business record + security event
  written in ONE MongoDB transaction. Kafka Connect CDC picks both up; this service
  never publishes to Kafka.

Dev identity: X-Actor / X-Principal headers (ZITADEL JWT validation replaces this
in the auth phase — the rest of the pipeline is unchanged).
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
SERVICE_ACCOUNT = "svc-commitment"

app = FastAPI(title="commitment-service")
client = MongoClient(MONGO_URI)
commitments = client["cos_commitments"]["commitments"]
security_events = client["security_events"]["commitment_service"]

VALID_TRANSITIONS = {
    "complete": ("OPEN", "DONE"),
    "slip": ("OPEN", "SLIPPED"),
    "drop": ("OPEN", "DROPPED"),
    "reopen": ("SLIPPED", "OPEN"),
}


class CommitmentIn(BaseModel):
    owner: str = Field(description="who owes it (email)")
    owed_to: str = Field(description="who it is owed to (email)")
    description: str
    due_at: str = Field(description="ISO date, e.g. 2026-08-01")
    project: str | None = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def authorize(actor: str, principal: str, action: str) -> dict:
    """OPA preflight. Deny -> 403. Returns {allow, allow_basis, tier}."""
    r = httpx.post(
        f"{OPA_URL}/v1/data/cos/authz",
        json={"input": {"actor": actor, "principal": principal, "action": action}},
        timeout=5,
    )
    r.raise_for_status()
    decision = r.json().get("result", {})
    if not decision.get("allow"):
        raise HTTPException(403, f"OPA denied '{action}' for {actor} on {principal}")
    return {
        "allow": True,
        "allowBasis": decision.get("allow_basis", "unspecified"),
        "tier": decision.get("tier", "none"),
    }


def write_txn(record: dict, event: dict) -> None:
    """Business record + security event in one multi-document transaction."""
    # insert copies: pymongo mutates the inserted dict (adds ObjectId _id)
    with client.start_session() as s:
        with s.start_transaction():
            commitments.insert_one(dict(record), session=s)
            security_events.insert_one(dict(event), session=s)


def security_event(actor: str, principal: str, action: str, authz: dict,
                   entity_id: str, version: int, details: dict) -> dict:
    return {
        "eventId": str(uuid.uuid4()),
        "principalId": principal,
        "actor": actor,
        "onBehalfOf": SERVICE_ACCOUNT,
        "action": action,
        "entityType": "commitment",
        "entityId": entity_id,
        "entityVersion": version,
        "occurredAt": now(),
        "authorization": authz,
        "details": details,
    }


@app.post("/commitments", status_code=201)
def create(body: CommitmentIn,
           x_actor: str = Header(...), x_principal: str = Header(...)):
    authz = authorize(x_actor, x_principal, "create_commitment")
    cid = f"C-{uuid.uuid4().hex[:8].upper()}"
    record = {
        "commitmentId": cid,
        "principalId": x_principal,
        "owner": body.owner,
        "owedTo": body.owed_to,
        "description": body.description,
        "project": body.project,
        "status": "OPEN",
        "dueAt": body.due_at,
        "slippedCount": 0,
        "version": 1,
        "createdAt": now(),
        "updatedAt": now(),
    }
    event = security_event(x_actor, x_principal, "create", authz, cid, 1,
                           {"description": body.description, "owner": body.owner})
    write_txn(record, event)
    record.pop("_id", None)
    return record


@app.post("/commitments/{cid}/{action}")
def transition(cid: str, action: str,
               x_actor: str = Header(...), x_principal: str = Header(...)):
    if action not in VALID_TRANSITIONS:
        raise HTTPException(400, f"unknown action '{action}'")
    required_from, to_status = VALID_TRANSITIONS[action]
    authz = authorize(x_actor, x_principal, "create_commitment")

    current = commitments.find_one(
        {"commitmentId": cid, "principalId": x_principal},
        sort=[("version", -1)],
    )
    if current is None:
        raise HTTPException(404, f"commitment {cid} not found")
    if current["status"] != required_from:
        raise HTTPException(409, f"{cid} is {current['status']}, need {required_from}")

    version = current["version"] + 1
    record = {**current, "status": to_status, "version": version, "updatedAt": now()}
    if action == "slip":
        record["slippedCount"] = current.get("slippedCount", 0) + 1
    record.pop("_id", None)
    event = security_event(x_actor, x_principal, action, authz, cid, version,
                           {"from": current["status"], "to": to_status})
    write_txn(record, event)
    return record


@app.get("/commitments")
def list_commitments(status: str | None = None,
                     x_actor: str = Header(...), x_principal: str = Header(...)):
    authorize(x_actor, x_principal, "create_commitment")
    match: dict = {"principalId": x_principal}
    if status:
        match["status"] = status.upper()
    pipeline = [
        {"$match": match},
        {"$sort": {"version": -1}},
        {"$group": {"_id": "$commitmentId", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$project": {"_id": 0}},
        {"$sort": {"dueAt": 1}},
    ]
    return list(commitments.aggregate(pipeline))


@app.get("/health")
def health():
    return {"ok": True}
