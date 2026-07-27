"""cos-chat — chat layer of the AI Chief of Staff.

Pattern: one routing decision per question (LLM structured output when
ANTHROPIC_API_KEY is set; deterministic fallback otherwise), then deterministic
execution — Cypher plans + formatters, or live OPA. No free-form agent loop.

Paths (phase 2):
  me          — my open / overdue commitments            -> Neo4j
  audit       — who did what to commitment X, on what authority -> Neo4j events
  eligibility — who CAN do action Y for the principal    -> live OPA
"""

import json
import os
import re
from datetime import date

import httpx
from fastapi import FastAPI, Header
from neo4j import GraphDatabase
from pydantic import BaseModel

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
NEO4J_AUTH = (os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "coslocal1"))
OPA_URL = os.environ.get("OPA_URL", "http://opa:8181")
ACTION_URL = os.environ.get("ACTION_SERVICE_URL", "http://action-service:8000")
DECISION_URL = os.environ.get("DECISION_SERVICE_URL", "http://decision-service:8000")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

app = FastAPI(title="cos-chat")
driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)

ACTIONS = ["draft_email", "send_email", "move_meeting", "decline_meeting",
           "assign_task", "record_decision", "create_commitment", "nudge_owner"]


class Question(BaseModel):
    question: str


# ---------------- routing ----------------

ROUTER_SYSTEM = f"""You route questions for a Chief of Staff assistant.
Return ONLY a JSON object: {{"path": "...", "commitmentId": null, "action": null,
"status": null, "personEmail": null, "meetingRef": null, "reasoning": "..."}}

Paths:
- "me": the asker's commitments / plate / what's due or overdue
- "audit": who DID something to a commitment or meeting, history, authority ("who created C-XXXX", "why was it dropped")
- "eligibility": who CAN perform an action (can/may/allowed/authorized). action must be one of {ACTIONS}
- "commitments": open items owed by or to a specific person, or filtered by status (OPEN/DONE/SLIPPED/DROPPED)
- "today": the schedule/calendar — what meetings are today / this week / coming up
- "prep": prepare or brief the asker for a meeting ("prep me for X", "get me ready for the 3pm")
- "brief": the daily / morning brief, digest, rundown of everything ("my morning brief",
  "give me the rundown", "what do I need to know today")
- "decisions": query decision memory — what was decided about a topic, who decided,
  why, whether it was reversed ("what did we decide about pricing", "why did we
  choose vendor X"). Set decisionQuery to the topic keywords.
- To RECORD a new decision ("record decision: we will ..."), use path "skill" with
  skillAction "record_decision" and put the decision text in "body" (rationale after
  "because" goes in "subject" as the basis).
- "skill": the asker wants the assistant to DO something: send/draft an email, nudge
  someone about a commitment. Set skillAction to send_email / draft_email / nudge_owner,
  and fill "to" (email), "subject", "body" when given. nudge_owner needs commitmentId.
- "confirm": go / no-go on a pending action ("confirm A-12AB34CD go", "cancel A-...").
  Set actionId and decision ("go" or "no_go").

Slots: commitmentId like C-ABC12345 if mentioned; actionId like A-ABC12345; status
uppercase; personEmail if a person is named by email; meetingRef = words identifying
the meeting for "prep" (title fragment), null for the next upcoming meeting.
Add to the JSON: "skillAction", "to", "subject", "body", "actionId", "decision"."""


def route_llm(question: str) -> dict | None:
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300,
                  "system": ROUTER_SYSTEM,
                  "messages": [{"role": "user", "content": question}]},
            timeout=20,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception as exc:
        print(f"router LLM failed, falling back: {exc}", flush=True)
        return None


def route_fallback(question: str) -> dict:
    """Deterministic fallback when no LLM key. Stable tokens only."""
    q = question.lower()
    cid = re.search(r"\bC-[A-Z0-9]{8}\b", question)
    decision: dict = {"path": "me", "commitmentId": cid.group(0) if cid else None,
                      "action": None, "status": None, "personEmail": None,
                      "meetingRef": None,
                      "reasoning": "fallback router (no ANTHROPIC_API_KEY)"}
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", question)
    if email:
        decision["personEmail"] = email.group(0)
    conf = re.search(r"\b(confirm|cancel)\s+(A-[A-Z0-9]{8})(?:\s+(go|no[_ ]?go))?", question, re.I)
    aid = re.search(r"\bA-[A-Z0-9]{8}\b", question)
    prep = re.search(r"prep(?:are)?(?: me)?(?: for)?\s+(.*)", q)
    if conf is not None or (aid and ("go" in q.split() or "cancel" in q)):
        decision["path"] = "confirm"
        decision["actionId"] = (conf.group(2) if conf else aid.group(0)).upper()
        word = (conf.group(3) or "") if conf else ""
        verb = (conf.group(1) if conf else "cancel").lower()
        decision["decision"] = ("no_go" if ("no" in word.lower() or verb == "cancel")
                                else "go")
    elif q.startswith("record decision"):
        decision["path"] = "skill"
        decision["skillAction"] = "record_decision"
        text = question.split(":", 1)[1].strip() if ":" in question else ""
        if " because " in text:
            what, why = text.split(" because ", 1)
            decision["body"], decision["subject"] = what.strip(), why.strip()
        else:
            decision["body"], decision["subject"] = text, None
    elif any(w in q for w in ("decide", "decision", "decided", "why did we choose")):
        decision["path"] = "decisions"
        decision["decisionQuery"] = None
        mref = re.search(r"(?:about|on|regarding|choose|decide[d]? to)\s+(.*)", q)
        if mref:
            ref = mref.group(1).strip(" ?.!")
            decision["decisionQuery"] = re.sub(r"^(the|a|an|our)\s+", "", ref) or None
    elif any(w in q for w in ("brief", "digest", "rundown", "need to know")):
        decision["path"] = "brief"
    elif any(w in q for w in ("who can", "who may", "allowed", "authorized", "eligible")):
        decision["path"] = "eligibility"
        decision["action"] = next((a for a in ACTIONS if a.replace("_", " ") in q or a in q), "send_email")
    elif "nudge" in q:
        decision["path"] = "skill"
        decision["skillAction"] = "nudge_owner"
    elif re.search(r"\b(send|draft|write)\b.*\b(email|mail|note)\b", q):
        decision["path"] = "skill"
        decision["skillAction"] = "send_email" if "send" in q else "draft_email"
        decision["subject"] = None
        decision["body"] = None
    elif prep is not None:
        decision["path"] = "prep"
        ref = prep.group(1).strip(" ?.!")
        decision["meetingRef"] = ref or None
    elif any(w in q for w in ("today", "calendar", "schedule", "meeting", "my day", "week", "upcoming")):
        decision["path"] = "today"
    elif any(w in q for w in ("who can", "who may", "allowed", "authorized", "eligible")):
        decision["path"] = "eligibility"
        decision["action"] = next((a for a in ACTIONS if a.replace("_", " ") in q or a in q), "send_email")
    elif cid or any(w in q for w in ("who created", "who did", "history", "audit", "authority", "why was")):
        decision["path"] = "audit"
    elif email or any(s in q for s in ("slipped", "dropped", "done")):
        decision["path"] = "commitments"
        for s in ("SLIPPED", "DROPPED", "DONE", "OPEN"):
            if s.lower() in q:
                decision["status"] = s
                break
    return decision


# ---------------- handlers (deterministic) ----------------

def handle_me(actor: str, principal: str) -> str:
    """Both directions: what I owe, and what others owe me (chase-worthy)."""
    today = str(date.today())
    with driver.session() as s:
        owed_by_me = s.run(
            """
            MATCH (p:Person {email: $actor, tenant: $tenant})-[:OWES]->(c:Commitment)
            WHERE c.status = 'OPEN'
            OPTIONAL MATCH (c)-[:OWED_TO]->(to:Person)
            RETURN c.id AS id, c.description AS what, c.dueAt AS due, to.email AS other
            ORDER BY c.dueAt
            """, actor=actor, tenant=principal).data()
        owed_to_me = s.run(
            """
            MATCH (owner:Person)-[:OWES]->(c:Commitment)-[:OWED_TO]->(p:Person {email: $actor, tenant: $tenant})
            WHERE c.status = 'OPEN' AND owner.email <> $actor
            RETURN c.id AS id, c.description AS what, c.dueAt AS due,
                   owner.email AS other, c.slippedCount AS slips
            ORDER BY c.dueAt
            """, actor=actor, tenant=principal).data()
    if not owed_by_me and not owed_to_me:
        return f"Nothing open in either direction, {actor}."
    lines = []
    if owed_by_me:
        lines.append(f"You owe ({len(owed_by_me)}):")
        for r in owed_by_me:
            overdue = " — OVERDUE" if (r["due"] or "9999") < today else ""
            lines.append(f"  {r['id']}  due {r['due']}{overdue}  to {r['other']}: {r['what']}")
    if owed_to_me:
        lines.append(f"Owed to you — worth chasing ({len(owed_to_me)}):")
        for r in owed_to_me:
            overdue = " — OVERDUE" if (r["due"] or "9999") < today else ""
            slip = f" (slipped {r['slips']}x)" if r.get("slips") else ""
            lines.append(f"  {r['id']}  due {r['due']}{overdue}{slip}  from {r['other']}: {r['what']}")
        lines.append("Tip: 'nudge <owner email> about <C-id>' sends a follow-up for you.")
    return "\n".join(lines)


def handle_commitments(principal: str, status: str | None, person: str | None) -> str:
    where, params = ["c.tenant = $tenant"], {"tenant": principal}
    if status:
        where.append("c.status = $status"); params["status"] = status
    else:
        where.append("c.status = 'OPEN'")
    if person:
        where.append("(owner.email = $person OR to.email = $person)"); params["person"] = person
    with driver.session() as s:
        rows = s.run(
            f"""
            MATCH (owner:Person)-[:OWES]->(c:Commitment)
            OPTIONAL MATCH (c)-[:OWED_TO]->(to:Person)
            WITH owner, c, to WHERE {' AND '.join(where)}
            RETURN c.id AS id, c.status AS status, c.dueAt AS due,
                   owner.email AS owner, to.email AS owedTo, c.description AS what
            ORDER BY c.dueAt
            """, **params).data()
    if not rows:
        return "No matching commitments."
    lines = [f"{len(rows)} commitment(s):"]
    for r in rows:
        lines.append(f"  {r['id']} [{r['status']}] due {r['due']}  {r['owner']} -> {r['owedTo']}: {r['what']}")
    return "\n".join(lines)


def handle_audit(principal: str, cid: str | None) -> str:
    if not cid:
        return "Which commitment? Give me its id (e.g. C-1A2B3C4D)."
    with driver.session() as s:
        rows = s.run(
            """
            MATCH (actor:Person)-[:PERFORMED]->(e:Event)-[:ON]->(c:Commitment {id: $cid, tenant: $tenant})
            RETURN e.at AS at, e.action AS action, actor.email AS actor,
                   e.allowBasis AS basis, e.entityVersion AS v
            ORDER BY e.at
            """, cid=cid, tenant=principal).data()
    if not rows:
        return f"No audit trail found for {cid}."
    lines = [f"Audit trail for {cid}:"]
    for r in rows:
        lines.append(f"  v{r['v']}  {r['at']}  {r['action']} by {r['actor']}  [basis: {r['basis']}]")
    return "\n".join(lines)


def handle_eligibility(principal: str, action: str | None) -> str:
    action = action or "send_email"
    r = httpx.post(f"{OPA_URL}/v1/data/cos/authz/eligible_actors",
                   json={"input": {"principal": principal, "action": action}}, timeout=5)
    actors = sorted(r.json().get("result", []))
    if not actors:
        return f"No one is currently authorized to {action.replace('_', ' ')} for {principal}."
    return (f"Authorized to {action.replace('_', ' ')} for {principal} (live policy): "
            + ", ".join(actors))


def format_card(payload: dict) -> str:
    c = payload["card"]
    r = payload["record"]
    lines = [f"{c['status']} — {c['action'].replace('_', ' ')} [{r['actionId']}]  (tier {c['tier']})"]
    lines.append(f"  To: {c['to']}")
    lines.append(f"  Subject: {c['subject']}")
    if c.get("body"):
        lines.append(f"  Body: {c['body'][:200]}")
    lines.append(f"  Policy basis: {c['policyBasis']}")
    if c["status"] == "PROPOSED":
        lines.append(f"  >> Reply 'confirm {r['actionId']} go' to execute, "
                     f"'confirm {r['actionId']} no go' to cancel.")
    elif c["status"] == "EXECUTED":
        lines.append("  Executed (outbox) and logged to the audit trail.")
    elif c["status"] == "DRAFT":
        lines.append("  Draft only — your access tier (T0) cannot execute this.")
    return "\n".join(lines)


def handle_decisions(actor: str, principal: str, d: dict) -> str:
    query = d.get("decisionQuery")
    try:
        r = httpx.get(f"{DECISION_URL}/decisions", params={"q": query} if query else {},
                      headers={"X-Actor": actor, "X-Principal": principal}, timeout=10)
        r.raise_for_status()
        rows = r.json()
    except httpx.HTTPStatusError as exc:
        return f"Cannot query decisions: {exc.response.json().get('detail', str(exc))}"
    if not rows:
        return (f"No decisions recorded about '{query}'." if query
                else "No decisions recorded yet. Say 'record decision: ... because ...' to add one.")
    lines = [f"Decisions{f' about {query}' if query else ''} ({len(rows)}):"]
    for dn in rows[:10]:
        lines.append(f"  {dn['decisionId']} [{dn['status']}] {dn['decidedAt'][:10]} "
                     f"by {dn['decidedBy']}: {dn['title']}")
        if dn.get("basis"):
            lines.append(f"      basis: {dn['basis']}")
        if dn.get("supersedes"):
            lines.append(f"      supersedes {dn['supersedes']}")
        if dn.get("revisitReason"):
            lines.append(f"      revisited: {dn['revisitReason']}")
    return "\n".join(lines)


def record_decision_skill(actor: str, principal: str, d: dict) -> str:
    title = (d.get("body") or "").strip()
    if not title:
        return "What was decided? Say: record decision: <what> because <why>."
    try:
        r = httpx.post(f"{DECISION_URL}/decisions",
                       json={"title": title, "basis": d.get("subject")},
                       headers={"X-Actor": actor, "X-Principal": principal}, timeout=10)
        if r.status_code == 403:
            return f"Policy denied: {r.json().get('detail')}"
        r.raise_for_status()
        dn = r.json()
        basis = f"\n  Basis: {dn['basis']}" if dn.get("basis") else ""
        return (f"Recorded {dn['decisionId']}: {dn['title']}{basis}\n"
                f"  Decided by {dn['decidedBy']} — on the audit trail.")
    except httpx.HTTPStatusError as exc:
        return f"Could not record decision: {exc.response.json().get('detail', str(exc))}"


def handle_skill(actor: str, principal: str, d: dict) -> str:
    skill = d.get("skillAction")
    if skill == "record_decision":
        return record_decision_skill(actor, principal, d)
    if skill not in ("send_email", "draft_email", "nudge_owner"):
        return "I can send_email, draft_email, nudge_owner, or record_decision. Which one?"
    body = {"type": skill, "to": d.get("to") or d.get("personEmail"),
            "subject": d.get("subject"), "body": d.get("body"),
            "commitment_id": d.get("commitmentId")}
    try:
        r = httpx.post(f"{ACTION_URL}/actions/propose", json=body,
                       headers={"X-Actor": actor, "X-Principal": principal}, timeout=10)
        if r.status_code == 403:
            return f"Policy denied: {r.json().get('detail', 'not authorized')}"
        r.raise_for_status()
        return format_card(r.json())
    except httpx.HTTPStatusError as exc:
        return f"Could not propose action: {exc.response.json().get('detail', str(exc))}"


def handle_confirm(actor: str, principal: str, d: dict) -> str:
    aid, decision = d.get("actionId"), d.get("decision")
    if not aid:
        return "Which action? Give me its id (e.g. A-1A2B3C4D)."
    try:
        r = httpx.post(f"{ACTION_URL}/actions/{aid}/confirm",
                       json={"decision": decision or "go"},
                       headers={"X-Actor": actor, "X-Principal": principal}, timeout=10)
        if r.status_code in (403, 404, 409):
            return f"Cannot confirm: {r.json().get('detail')}"
        r.raise_for_status()
        return format_card(r.json())
    except httpx.HTTPStatusError as exc:
        return f"Confirm failed: {exc.response.json().get('detail', str(exc))}"


def handle_today(principal: str) -> str:
    today = str(date.today())
    with driver.session() as s:
        rows = s.run(
            """
            MATCH (m:Meeting {tenant: $tenant})
            WHERE m.startAt >= $today AND m.status = 'SCHEDULED'
            OPTIONAL MATCH (p:Person)-[:ATTENDS]->(m)
            RETURN m.id AS id, m.title AS title, m.startAt AS start,
                   m.location AS loc, collect(DISTINCT p.email) AS people
            ORDER BY m.startAt LIMIT 15
            """, tenant=principal, today=today).data()
    if not rows:
        return "No upcoming meetings on the calendar."
    lines = [f"Upcoming meetings ({len(rows)}):"]
    for r in rows:
        who = f" — with {', '.join(r['people'][:4])}" if r["people"] else ""
        loc = f" @ {r['loc']}" if r["loc"] else ""
        lines.append(f"  {r['start'][:16]}  {r['title']}{loc}{who}  [{r['id']}]")
    return "\n".join(lines)


def handle_prep(principal: str, meeting_ref: str | None) -> str:
    """Prep brief: the meeting, its people, and every open thread with those people."""
    today = str(date.today())
    with driver.session() as s:
        if meeting_ref:
            m = s.run(
                """
                MATCH (m:Meeting {tenant: $tenant})
                WHERE toLower(m.title) CONTAINS toLower($ref) AND m.startAt >= $today
                RETURN m.id AS id, m.title AS title, m.startAt AS start, m.location AS loc
                ORDER BY m.startAt LIMIT 1
                """, tenant=principal, ref=meeting_ref, today=today).single()
        else:
            m = s.run(
                """
                MATCH (m:Meeting {tenant: $tenant})
                WHERE m.startAt >= $today AND m.status = 'SCHEDULED'
                RETURN m.id AS id, m.title AS title, m.startAt AS start, m.location AS loc
                ORDER BY m.startAt LIMIT 1
                """, tenant=principal, today=today).single()
        if m is None:
            return ("No matching upcoming meeting found."
                    if meeting_ref else "No upcoming meetings to prep for.")

        attendees = [r["email"] for r in s.run(
            "MATCH (p:Person)-[:ATTENDS]->(:Meeting {id: $id, tenant: $tenant}) RETURN p.email AS email",
            id=m["id"], tenant=principal)]

        open_items = s.run(
            """
            MATCH (owner:Person)-[:OWES]->(c:Commitment {tenant: $tenant})-[:OWED_TO]->(to:Person)
            WHERE c.status = 'OPEN' AND (owner.email IN $people OR to.email IN $people)
            RETURN c.id AS id, c.description AS what, c.dueAt AS due,
                   owner.email AS owner, to.email AS owedTo, c.slippedCount AS slips
            ORDER BY c.dueAt
            """, tenant=principal, people=attendees).data()

        history = s.run(
            """
            MATCH (p:Person)-[:ATTENDS]->(m:Meeting {tenant: $tenant})
            WHERE p.email IN $people AND m.startAt < $today
            RETURN DISTINCT m.title AS title, m.startAt AS start
            ORDER BY m.startAt DESC LIMIT 5
            """, tenant=principal, people=attendees, today=today).data()

    lines = [f"PREP — {m['title']}  ({m['start'][:16]}{' @ ' + m['loc'] if m['loc'] else ''})"]
    lines.append(f"Attendees: {', '.join(attendees) if attendees else 'none listed'}")
    if open_items:
        lines.append(f"\nOpen threads with these people ({len(open_items)}):")
        for c in open_items:
            slip = f"  (slipped {c['slips']}x)" if c.get("slips") else ""
            overdue = " OVERDUE" if (c["due"] or "9999") < today else ""
            lines.append(f"  {c['id']} due {c['due']}{overdue}{slip}: {c['owner']} -> {c['owedTo']}: {c['what']}")
    else:
        lines.append("\nNo open commitments involving these attendees.")
    if history:
        lines.append("\nRecent meetings with them:")
        for h in history:
            lines.append(f"  {h['start'][:10]}  {h['title']}")
    return "\n".join(lines)


def narrative(brief_text: str) -> str | None:
    """Optional: one short LLM paragraph naming the top priorities."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        r = httpx.post("https://api.anthropic.com/v1/messages",
                       headers={"x-api-key": ANTHROPIC_API_KEY,
                                "anthropic-version": "2023-06-01"},
                       json={"model": "claude-haiku-4-5-20251001", "max_tokens": 250,
                             "system": ("You are a chief of staff. Given today's brief, write 2-3 "
                                        "sentences naming the top priorities and biggest risk. "
                                        "Plain text, no preamble, refer only to items in the brief."),
                             "messages": [{"role": "user", "content": brief_text}]},
                       timeout=20)
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    except Exception as exc:
        print(f"brief narrative failed: {exc}", flush=True)
        return None


def handle_brief(actor: str, principal: str) -> str:
    today = str(date.today())
    with driver.session() as s:
        meetings = s.run(
            """
            MATCH (m:Meeting {tenant: $tenant})
            WHERE m.startAt STARTS WITH $today AND m.status = 'SCHEDULED'
            OPTIONAL MATCH (p:Person)-[:ATTENDS]->(m)
            RETURN m.title AS title, m.startAt AS start, m.location AS loc,
                   collect(DISTINCT p.email) AS people
            ORDER BY m.startAt
            """, tenant=principal, today=today).data()
        overdue = s.run(
            """
            MATCH (owner:Person)-[:OWES]->(c:Commitment {tenant: $tenant})-[:OWED_TO]->(to:Person)
            WHERE c.status = 'OPEN' AND c.dueAt < $today
            RETURN c.id AS id, c.description AS what, c.dueAt AS due,
                   owner.email AS owner, to.email AS owedTo
            ORDER BY c.dueAt
            """, tenant=principal, today=today).data()
        due_soon = s.run(
            """
            MATCH (owner:Person)-[:OWES]->(c:Commitment {tenant: $tenant})-[:OWED_TO]->(to:Person)
            WHERE c.status = 'OPEN' AND c.dueAt >= $today AND c.dueAt <= $soon
            RETURN c.id AS id, c.description AS what, c.dueAt AS due,
                   owner.email AS owner, to.email AS owedTo
            ORDER BY c.dueAt
            """, tenant=principal, today=today,
            soon=str(date.fromordinal(date.today().toordinal() + 3))).data()
        slipped = s.run(
            """
            MATCH (owner:Person)-[:OWES]->(c:Commitment {tenant: $tenant})
            WHERE c.status = 'OPEN' AND c.slippedCount > 0
            RETURN c.id AS id, c.description AS what, c.slippedCount AS slips,
                   owner.email AS owner
            ORDER BY c.slippedCount DESC
            """, tenant=principal).data()
        pending = s.run(
            """
            MATCH (p:Person)-[:PROPOSED]->(a:Action {tenant: $tenant})
            WHERE a.status = 'PROPOSED'
            RETURN a.id AS id, a.type AS type, a.to AS to, a.subject AS subject,
                   p.email AS by
            ORDER BY a.updatedAt
            """, tenant=principal).data()

    lines = [f"MORNING BRIEF — {today}", ""]
    lines.append(f"Today's meetings ({len(meetings)}):")
    if meetings:
        for m in meetings:
            who = f" — {', '.join(m['people'][:4])}" if m["people"] else ""
            lines.append(f"  {m['start'][11:16]}  {m['title']}"
                         f"{' @ ' + m['loc'] if m['loc'] else ''}{who}")
    else:
        lines.append("  clear calendar")
    lines.append("")
    if overdue:
        lines.append(f"OVERDUE ({len(overdue)}):")
        for c in overdue:
            lines.append(f"  {c['id']} was due {c['due']}: {c['owner']} -> {c['owedTo']}: {c['what']}")
        lines.append("")
    if due_soon:
        lines.append(f"Due in the next 3 days ({len(due_soon)}):")
        for c in due_soon:
            lines.append(f"  {c['id']} due {c['due']}: {c['owner']} -> {c['owedTo']}: {c['what']}")
        lines.append("")
    if slipped:
        lines.append(f"Slipped before — watch these ({len(slipped)}):")
        for c in slipped:
            lines.append(f"  {c['id']} (slipped {c['slips']}x) {c['owner']}: {c['what']}")
        lines.append("")
    if pending:
        lines.append(f"Awaiting your Go/No-Go ({len(pending)}):")
        for a in pending:
            lines.append(f"  {a['id']} {a['type']} to {a['to']} ({a['subject']}) — proposed by {a['by']}")
        lines.append("")
    if not (overdue or due_soon or slipped or pending):
        lines.append("No open risks: nothing overdue, slipped, or awaiting confirmation.")
    body = "\n".join(lines).rstrip()
    top = narrative(body)
    return (f"{body}\n\nTOP OF MIND: {top}" if top else body)


# ---------------- api ----------------

@app.post("/chat")
def chat(q: Question, x_actor: str = Header(...), x_principal: str = Header(...)):
    decision = (route_llm(q.question) if ANTHROPIC_API_KEY else None) or route_fallback(q.question)
    path = decision.get("path", "me")
    if path == "me":
        answer = handle_me(x_actor, x_principal)
    elif path == "audit":
        answer = handle_audit(x_principal, decision.get("commitmentId"))
    elif path == "eligibility":
        answer = handle_eligibility(x_principal, decision.get("action"))
    elif path == "commitments":
        answer = handle_commitments(x_principal, decision.get("status"), decision.get("personEmail"))
    elif path == "today":
        answer = handle_today(x_principal)
    elif path == "prep":
        answer = handle_prep(x_principal, decision.get("meetingRef"))
    elif path == "brief":
        answer = handle_brief(x_actor, x_principal)
    elif path == "decisions":
        answer = handle_decisions(x_actor, x_principal, decision)
    elif path == "skill":
        answer = handle_skill(x_actor, x_principal, decision)
    elif path == "confirm":
        answer = handle_confirm(x_actor, x_principal, decision)
    else:
        answer = f"Path '{path}' not implemented yet."
    return {"answer": answer, "routing": decision}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def ui():
    from fastapi.responses import FileResponse
    return FileResponse("ui.html", media_type="text/html")
