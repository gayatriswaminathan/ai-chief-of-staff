"""cos-indexer — consumes CDC topics, denormalizes into the Neo4j graph.

Self-contained (full documents embedded in each message — no API callbacks).
Pipelines (phase 2): commitments, commitment_security_events.

Graph model:
  (Person {email, tenant})-[:OWES]->(Commitment {id, tenant, ...})-[:OWED_TO]->(Person)
  (Commitment)-[:FOR]->(Project {name, tenant})
  (Person)-[:PERFORMED]->(Event {id, action, at, allowBasis})-[:ON]->(Commitment)
"""

import json
import os
import time

from kafka import KafkaConsumer
from neo4j import GraphDatabase

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:19092")
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
NEO4J_AUTH = (os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "coslocal1"))
TOPICS = ["commitments", "commitment_security_events",
          "meetings", "meeting_security_events",
          "actions", "action_security_events",
          "decisions", "decision_security_events"]

driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)


def clean(value):
    """Normalize Mongo extended JSON scalars ({'$date': ...}, {'$oid': ...})."""
    if isinstance(value, dict):
        if "$date" in value:
            return str(value["$date"])
        if "$oid" in value:
            return str(value["$oid"])
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def upsert_commitment(tx, d):
    tx.run(
        """
        MERGE (c:Commitment {id: $id, tenant: $tenant})
        SET c.description = $description, c.status = $status, c.dueAt = $dueAt,
            c.slippedCount = $slippedCount, c.version = $version, c.updatedAt = $updatedAt
        MERGE (owner:Person {email: $owner, tenant: $tenant})
        MERGE (owedTo:Person {email: $owedTo, tenant: $tenant})
        MERGE (owner)-[:OWES]->(c)
        MERGE (c)-[:OWED_TO]->(owedTo)
        WITH c
        CALL {
          WITH c
          WITH c WHERE $project IS NOT NULL
          MERGE (p:Project {name: $project, tenant: c.tenant})
          MERGE (c)-[:FOR]->(p)
        }
        """,
        id=d["commitmentId"], tenant=d["principalId"],
        description=d.get("description"), status=d.get("status"),
        dueAt=d.get("dueAt"), slippedCount=d.get("slippedCount", 0),
        version=d.get("version", 1), updatedAt=d.get("updatedAt"),
        owner=d.get("owner"), owedTo=d.get("owedTo"), project=d.get("project"),
    )


def upsert_event(tx, d):
    authz = d.get("authorization", {})
    tx.run(
        """
        MERGE (e:Event {id: $id})
        SET e.tenant = $tenant, e.action = $action, e.at = $at,
            e.allowBasis = $allowBasis, e.tier = $tier, e.entityVersion = $version
        MERGE (actor:Person {email: $actor, tenant: $tenant})
        MERGE (actor)-[:PERFORMED]->(e)
        MERGE (c:Commitment {id: $entityId, tenant: $tenant})
        MERGE (e)-[:ON]->(c)
        """,
        id=d["eventId"], tenant=d["principalId"], action=d.get("action"),
        at=d.get("occurredAt"), allowBasis=authz.get("allowBasis"),
        tier=authz.get("tier"), version=d.get("entityVersion"),
        actor=d.get("actor"), entityId=d.get("entityId"),
    )


def upsert_meeting(tx, d):
    tx.run(
        """
        MERGE (m:Meeting {id: $id, tenant: $tenant})
        SET m.title = $title, m.startAt = $startAt, m.endAt = $endAt,
            m.location = $location, m.status = $status, m.source = $source,
            m.hasNotes = $hasNotes, m.version = $version
        WITH m
        UNWIND $attendees AS email
        MERGE (p:Person {email: email, tenant: $tenant})
        MERGE (p)-[:ATTENDS]->(m)
        """,
        id=d["meetingId"], tenant=d["principalId"], title=d.get("title"),
        startAt=d.get("startAt"), endAt=d.get("endAt"), location=d.get("location"),
        status=d.get("status"), source=d.get("source"),
        hasNotes=bool(d.get("notes")), version=d.get("version", 1),
        attendees=[a for a in (d.get("attendees") or []) if a],
    )


def upsert_meeting_event(tx, d):
    authz = d.get("authorization", {})
    tx.run(
        """
        MERGE (e:Event {id: $id})
        SET e.tenant = $tenant, e.action = $action, e.at = $at,
            e.allowBasis = $allowBasis, e.tier = $tier, e.entityVersion = $version
        MERGE (actor:Person {email: $actor, tenant: $tenant})
        MERGE (actor)-[:PERFORMED]->(e)
        MERGE (m:Meeting {id: $entityId, tenant: $tenant})
        MERGE (e)-[:ON]->(m)
        """,
        id=d["eventId"], tenant=d["principalId"], action=d.get("action"),
        at=d.get("occurredAt"), allowBasis=authz.get("allowBasis"),
        tier=authz.get("tier"), version=d.get("entityVersion"),
        actor=d.get("actor"), entityId=d.get("entityId"),
    )


def upsert_action(tx, d):
    payload = d.get("payload") or {}
    tx.run(
        """
        MERGE (a:Action {id: $id, tenant: $tenant})
        SET a.type = $type, a.status = $status, a.tier = $tier,
            a.to = $to, a.subject = $subject, a.allowBasis = $allowBasis,
            a.version = $version, a.updatedAt = $updatedAt
        MERGE (p:Person {email: $proposedBy, tenant: $tenant})
        MERGE (p)-[:PROPOSED]->(a)
        WITH a
        CALL {
          WITH a
          WITH a WHERE $commitmentId IS NOT NULL
          MERGE (c:Commitment {id: $commitmentId, tenant: a.tenant})
          MERGE (a)-[:TARGETS]->(c)
        }
        """,
        id=d["actionId"], tenant=d["principalId"], type=d.get("type"),
        status=d.get("status"), tier=d.get("tier"), to=payload.get("to"),
        subject=payload.get("subject"), allowBasis=d.get("allowBasis"),
        version=d.get("version", 1), updatedAt=d.get("updatedAt"),
        proposedBy=d.get("proposedBy"), commitmentId=payload.get("commitmentId"),
    )


def upsert_action_event(tx, d):
    authz = d.get("authorization", {})
    tx.run(
        """
        MERGE (e:Event {id: $id})
        SET e.tenant = $tenant, e.action = $action, e.at = $at,
            e.allowBasis = $allowBasis, e.tier = $tier,
            e.confirmation = $confirmation, e.entityVersion = $version
        MERGE (actor:Person {email: $actor, tenant: $tenant})
        MERGE (actor)-[:PERFORMED]->(e)
        MERGE (a:Action {id: $entityId, tenant: $tenant})
        MERGE (e)-[:ON]->(a)
        """,
        id=d["eventId"], tenant=d["principalId"], action=d.get("action"),
        at=d.get("occurredAt"), allowBasis=authz.get("allowBasis"),
        tier=authz.get("tier"), confirmation=authz.get("confirmation"),
        version=d.get("entityVersion"), actor=d.get("actor"), entityId=d.get("entityId"),
    )


def upsert_decision(tx, d):
    tx.run(
        """
        MERGE (dn:Decision {id: $id, tenant: $tenant})
        SET dn.title = $title, dn.basis = $basis, dn.status = $status,
            dn.decidedAt = $decidedAt, dn.version = $version
        MERGE (p:Person {email: $decidedBy, tenant: $tenant})
        MERGE (dn)-[:DECIDED_BY]->(p)
        WITH dn
        CALL {
          WITH dn
          WITH dn WHERE $supersedes IS NOT NULL
          MERGE (old:Decision {id: $supersedes, tenant: dn.tenant})
          MERGE (dn)-[:SUPERSEDES]->(old)
        }
        WITH dn
        CALL {
          WITH dn
          WITH dn WHERE $meetingId IS NOT NULL
          MERGE (m:Meeting {id: $meetingId, tenant: dn.tenant})
          MERGE (m)-[:PRODUCED]->(dn)
        }
        """,
        id=d["decisionId"], tenant=d["principalId"], title=d.get("title"),
        basis=d.get("basis"), status=d.get("status"), decidedAt=d.get("decidedAt"),
        version=d.get("version", 1), decidedBy=d.get("decidedBy"),
        supersedes=d.get("supersedes"), meetingId=d.get("meetingId"),
    )


def upsert_decision_event(tx, d):
    authz = d.get("authorization", {})
    tx.run(
        """
        MERGE (e:Event {id: $id})
        SET e.tenant = $tenant, e.action = $action, e.at = $at,
            e.allowBasis = $allowBasis, e.tier = $tier, e.entityVersion = $version
        MERGE (actor:Person {email: $actor, tenant: $tenant})
        MERGE (actor)-[:PERFORMED]->(e)
        MERGE (dn:Decision {id: $entityId, tenant: $tenant})
        MERGE (e)-[:ON]->(dn)
        """,
        id=d["eventId"], tenant=d["principalId"], action=d.get("action"),
        at=d.get("occurredAt"), allowBasis=authz.get("allowBasis"),
        tier=authz.get("tier"), version=d.get("entityVersion"),
        actor=d.get("actor"), entityId=d.get("entityId"),
    )


HANDLERS = {
    "commitments": ("commitmentId", upsert_commitment),
    "commitment_security_events": ("eventId", upsert_event),
    "meetings": ("meetingId", upsert_meeting),
    "meeting_security_events": ("eventId", upsert_meeting_event),
    "actions": ("actionId", upsert_action),
    "action_security_events": ("eventId", upsert_action_event),
    "decisions": ("decisionId", upsert_decision),
    "decision_security_events": ("eventId", upsert_decision_event),
}


def main():
    consumer = None
    while consumer is None:
        try:
            consumer = KafkaConsumer(
                *TOPICS,
                bootstrap_servers=BOOTSTRAP,
                group_id="cos-indexer",
                auto_offset_reset="earliest",
                value_deserializer=lambda b: b.decode("utf-8", "replace"),
            )
        except Exception as exc:  # kafka not up yet
            print(f"kafka not ready ({exc}); retrying in 5s", flush=True)
            time.sleep(5)

    print(f"cos-indexer consuming {TOPICS}", flush=True)
    for msg in consumer:
        try:
            raw = msg.value
            doc = json.loads(raw)
            if isinstance(doc, str):  # double-encoded by connector
                doc = json.loads(doc)
            doc = clean(doc)
            key_field, handler = HANDLERS[msg.topic]
            if key_field not in doc:
                print(f"skip {msg.topic}: no {key_field} in {str(doc)[:120]}", flush=True)
                continue
            with driver.session() as s:
                s.execute_write(handler, doc)
            print(f"indexed {msg.topic}/{doc[key_field]}", flush=True)
        except Exception as exc:
            print(f"ERROR {msg.topic}@{msg.offset}: {exc}", flush=True)


if __name__ == "__main__":
    main()
