"""action-service — delegated actions the CoS performs for the principal.

The trust model, end to end:
  1. propose  -> OPA preflight returns allow + basis + autonomy tier
     T0 (draft-only): recorded as DRAFT, can never be executed by this actor
     T1 (confirm):    recorded as PROPOSED, awaits explicit Go / No-Go
     T2 (auto):       executed immediately, logged with confirmation="auto"
  2. confirm  -> Go executes (OPA re-checked at execution time), No-Go cancels.

Execution is an OUTBOX in the prototype: the rendered email / nudge is stored on
the action record (CDC ships it to the graph) instead of hitting a live mail
server. Swapping in SMTP / Graph API later changes only `execute_payload`.

Every mutation writes the action version + security event in ONE Mongo txn.
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
COMMITMENT_URL = os.environ.get("COMMITMENT_SERVICE_URL", "http://commitment-service:8000")
SERVICE_ACCOUNT = "svc-action"

app = FastAPI(title="action-service")
client = MongoClient(MONGO_URI)
actions = client["cos_actions"]["actions"]
security_events = client["security_events"]["action_service"]

SUPPORTED = {"send_email", "draft_email", "nudge_owner"}


class ProposeIn(BaseModel):
    type: str = Field(description="send_email | draft_email | nudge_owner")
    to: str | None = None
    subject: str | None = None
    body: str | None = None
    commitment_id: str | None = None


class ConfirmIn(BaseModel):
    decision: str = Field(description="go | no_go")


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
    # insert copies: pymongo mutates the dict it inserts (adds ObjectId _id),
    # which would break JSON serialization of the API response
    with client.start_session() as s:
        with s.start_transaction():
            actions.insert_one(dict(record), session=s)
            security_events.insert_one(dict(event), session=s)


def security_event(actor, principal, action, authz, entity_id, version,
                   details, confirmation=None) -> dict:
    a = dict(authz)
    if confirmation:
        a["confirmation"] = confirmation
    return {"eventId": str(uuid.uuid4()), "principalId": principal, "actor": actor,
            "onBehalfOf": SERVICE_ACCOUNT, "action": action, "entityType": "action",
            "entityId": entity_id, "entityVersion": version, "occurredAt": now(),
            "authorization": a, "details": details}


def latest(principal: str, aid: str):
    return actions.find_one({"actionId": aid, "principalId": principal},
                            sort=[("version", -1)])


def build_payload(body: ProposeIn, actor: str, principal: str) -> dict:
    """Render exactly what would leave the building — shown on the confirm card."""
    if body.type == "nudge_owner":
        if not body.commitment_id:
            raise HTTPException(400, "nudge_owner needs commitment_id")
        r = httpx.get(f"{COMMITMENT_URL}/commitments",
                      headers={"X-Actor": actor, "X-Principal": principal}, timeout=5)
        r.raise_for_status()
        c = next((c for c in r.json() if c["commitmentId"] == body.commitment_id), None)
        if c is None:
            raise HTTPException(404, f"commitment {body.commitment_id} not found")
        return {
            "to": c["owner"],
            "subject": f"Nudge: {c['commitmentId']} due {c['dueAt']}",
            "body": (body.body or f"Checking in on \"{c['description']}\" "
                     f"(due {c['dueAt']}, owed to {c['owedTo']}). Where does it stand?"),
            "commitmentId": c["commitmentId"],
        }
    if not body.to:
        raise HTTPException(400, f"{body.type} needs 'to'")
    return {"to": body.to, "subject": body.subject or "(no subject)",
            "body": body.body or "", "commitmentId": body.commitment_id}


def execute_payload(record: dict) -> dict:
    """Prototype outbox: mark delivered locally. Real SMTP/Graph adapter goes here."""
    return {"delivered": "outbox", "deliveredAt": now()}


def card(record: dict) -> dict:
    p = record["payload"]
    return {
        "action": record["type"],
        "to": p.get("to"),
        "subject": p.get("subject"),
        "body": p.get("body"),
        "tier": record["tier"],
        "policyBasis": record["allowBasis"],
        "status": record["status"],
        "next": (f"POST /actions/{record['actionId']}/confirm with decision go|no_go"
                 if record["status"] == "PROPOSED" else None),
    }


@app.post("/actions/propose", status_code=201)
def propose(body: ProposeIn,
            x_actor: str = Header(...), x_principal: str = Header(...)):
    if body.type not in SUPPORTED:
        raise HTTPException(400, f"unsupported action type '{body.type}'")
    authz = authorize(x_actor, x_principal, body.type)
    tier = authz["tier"]
    payload = build_payload(body, x_actor, x_principal)

    aid = f"A-{uuid.uuid4().hex[:8].upper()}"
    status = {"T0": "DRAFT", "T1": "PROPOSED", "T2": "EXECUTED"}.get(tier, "PROPOSED")
    record = {"actionId": aid, "principalId": x_principal, "type": body.type,
              "payload": payload, "status": status, "tier": tier,
              "allowBasis": authz["allowBasis"], "proposedBy": x_actor,
              "result": execute_payload({}) if status == "EXECUTED" else None,
              "version": 1, "createdAt": now(), "updatedAt": now()}
    event = security_event(
        x_actor, x_principal,
        "execute" if status == "EXECUTED" else "propose",
        authz, aid, 1, {"type": body.type, "to": payload.get("to")},
        confirmation="auto" if status == "EXECUTED" else None)
    write_txn(record, event)
    record.pop("_id", None)
    return {"record": record, "card": card(record)}


@app.post("/actions/{aid}/confirm")
def confirm(aid: str, body: ConfirmIn,
            x_actor: str = Header(...), x_principal: str = Header(...)):
    if body.decision not in ("go", "no_go"):
        raise HTTPException(400, "decision must be 'go' or 'no_go'")
    current = latest(x_principal, aid)
    if current is None:
        raise HTTPException(404, f"action {aid} not found")
    if current["status"] != "PROPOSED":
        raise HTTPException(409, f"{aid} is {current['status']}; only PROPOSED can be confirmed")

    # Re-evaluate policy at execution time — the grant may have changed since propose.
    authz = authorize(x_actor, x_principal, current["type"])
    if body.decision == "go" and authz["tier"] == "T0":
        raise HTTPException(403, f"{x_actor} holds draft-only (T0) for {current['type']}")

    version = current["version"] + 1
    if body.decision == "go":
        record = {**current, "status": "EXECUTED", "result": execute_payload(current),
                  "confirmedBy": x_actor, "version": version, "updatedAt": now()}
        ev_action, conf = "execute", "go"
    else:
        record = {**current, "status": "CANCELLED", "confirmedBy": x_actor,
                  "version": version, "updatedAt": now()}
        ev_action, conf = "cancel", "no_go"
    record.pop("_id", None)
    event = security_event(x_actor, x_principal, ev_action, authz, aid, version,
                           {"type": current["type"]}, confirmation=conf)
    write_txn(record, event)
    return {"record": record, "card": card(record)}


@app.get("/actions")
def list_actions(status: str | None = None,
                 x_actor: str = Header(...), x_principal: str = Header(...)):
    authorize(x_actor, x_principal, "draft_email")
    match: dict = {"principalId": x_principal}
    if status:
        match["status"] = status.upper()
    pipeline = [
        {"$match": match},
        {"$sort": {"version": -1}},
        {"$group": {"_id": "$actionId", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$project": {"_id": 0}},
        {"$sort": {"updatedAt": -1}},
    ]
    return list(actions.aggregate(pipeline))


@app.get("/health")
def health():
    return {"ok": True}
