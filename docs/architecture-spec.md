# AI Chief of Staff — Architecture Spec

Leader-agnostic AI Chief of Staff: MongoDB + Kafka CDC event sourcing, Neo4j graph + vector retrieval, LLM semantic routing with deterministic execution, and OPA-governed delegated actions.

**Design rule — one system, any leader.** The software is built for a generic role called the *principal* (the leader it works for), never for a specific person. The code contains no names and no per-person logic. Everything that makes the system "yours" lives in settings, not source code:

- **Who you are** → a user record in the identity system (ZITADEL)
- **What your team may do for you** — who can draft vs. send, what runs automatically → a policy file (OPA)
- **How you like things** — briefing time, VIP contacts, meeting priorities → configuration

So onboarding a new leader means adding a user, loading a policy file, and setting preferences — no code changes, no redeployment. The same installation can serve one leader or a hundred.

---

## 0. Key concepts (read this first)

| Term | What it is | Role here |
|------|-----------|-----------|
| **Principal** | The leader an instance serves — an abstract role, not a person. Code only ever refers to "the principal." | Makes the system leader-agnostic. |
| **ZITADEL** | Open-source identity provider (self-hostable Auth0/Okta alternative). Stores users and roles, handles login/SSO/MFA, issues signed JWTs, supports service accounts. | Answers **"who are you?"** Every API call carries a ZITADEL JWT. Swappable for a corporate IdP (e.g., Entra ID) since services only consume standard JWTs. |
| **OPA** (Open Policy Agent) | Open-source policy engine. Services ask it "may user X do action Y for principal P?"; it evaluates Rego rules against per-principal data documents and returns `allow` + `allow_basis` (which rule permitted it). | Answers **"what are you allowed to do?"** Rules are generic and ship with the system; per-leader specifics are data files. The `allow_basis` is written into every security event — provable authority for every action. |
| **JWT / OBO** | JWT: signed token proving identity. OBO (On-Behalf-Of): a service account calls another service carrying the acting user's token, so the end user's identity — not just the service's — is checked at every hop. | No service trusts another blindly; authorization is always evaluated for the real human actor. |
| **Event sourcing + security events** | Every mutation writes two things atomically: the business record (versioned, append-only) and a security event capturing who/what/when/on-what-authority. | The audit trail is a property of the write path, not a bolt-on log. |
| **CDC** (Change Data Capture) | Kafka Connect watches MongoDB collections and streams every insert to Kafka topics. | Services never publish events themselves — the database is the single source of truth and downstream consumers can never miss or invent an event. |
| **Graph + vector retrieval** | Neo4j stores entities and relationships (who attended what, who owes whom); a vector index stores embeddings of the same records for semantic search. | Structured questions use the graph; open narrative questions use vectors; the router picks per question. |
| **`RouterDecision`** | One LLM call per question returning a strict JSON schema: an intent `path` plus extracted slots. Everything after is deterministic code. | The LLM classifies; it does not free-wheel. No agent loop, no regex intent guessing. |
| **Skills + autonomy tiers** | A skill is a scripted pipeline for one delegated action (send email, move meeting). Tiers set how far it may go per principal per action: **T0** draft-only, **T1** execute after Go/No-Go confirmation, **T2** execute automatically and log. | Delegation depth is policy data, tunable per leader without code changes. |

## 1. Principles

1. **Event-sourced truth with transactional audit.** Every mutation writes the business record *and* a security event in one MongoDB transaction. Domain services never publish to Kafka directly — CDC does.
2. **LLM routes, code executes.** One structured `RouterDecision` call per question; everything after is deterministic (graph queries, formatters, scripted skills). No free-form agent loop.
3. **Selective retrieval.** Graph, vector, or hybrid — chosen by the router, never merged blindly.
4. **Fast paths skip RAG.** Known question shapes hit Neo4j direct plans or live APIs with deterministic formatters.
5. **Mutations are scripted skills with preflight.** OPA policy check → Go/No-Go confirmation card → execute. Never a free-form tool loop.
6. **Eligibility vs audit.** "Who *can* act for the principal" is live OPA; "who *did* what" is the graph.

## 2. Domain model

| Entity | Description |
|--------|-------------|
| `Principal` | The leader the instance serves. Pure configuration — a ZITADEL subject + OPA role binding. Multi-tenant: one deployment, many principals. |
| `Person` | Anyone in the principal's orbit: directs, peers, externals. Nodes in the graph. |
| `Meeting` | Calendar event + notes + transcript refs + extracted action items. |
| `Decision` | Explicit record: what was decided, by whom, basis, alternatives considered, status (PROPOSED / DECIDED / REVISITED / REVERSED). |
| `Commitment` | A promise: who owes what to whom, due when, status (OPEN / DONE / SLIPPED / DROPPED). |
| `Project` | Workstream linking meetings, decisions, commitments, people. |
| `Communication` | Email/message drafts and sends executed on behalf of the principal. |
| `Brief` | Generated artifact (meeting prep, daily brief) — stored and versioned like any other record. |

Every entity is **versioned** (append-only versions) so "what did we know on March 3rd" is answerable.

## 3. Services (write side)

Four Spring Boot domain services, each following the same pattern: ZITADEL JWT validation → On-Behalf-Of call to authorization-service → OPA `allow` + `allow_basis` → record + security event in one Mongo transaction.

| Service | Mutations | Mongo collections |
|---------|-----------|-------------------|
| **meeting-service** | ingest event, attach notes/transcript, extract action items, generate prep brief | `cos_meetings.meetings`, `security_events.meeting_service` |
| **decision-service** | propose, record, revisit, reverse decision | `cos_decisions.decisions`, `security_events.decision_service` |
| **commitment-service** | create, update, complete, slip, drop commitment | `cos_commitments.commitments`, `security_events.commitment_service` |
| **action-service** | draft/send email, move/decline meeting, assign task — the *delegated actions* executor | `cos_actions.actions`, `security_events.action_service` |

The security event carries `details.authorization` (who acted, on behalf of which principal, which policy basis allowed it). This is the audit spine: every action the CoS takes for a principal is provably authorized and reconstructable.

**Service accounts:** `svc-meeting`, `svc-decision`, `svc-commitment`, `svc-action` — each calls authorization-service on-behalf-of with the acting user's token.

## 4. Authorization: leader-agnostic delegation

ZITADEL holds users and roles; OPA holds policy. The delegation model is the leader-agnostic core:

| Role | Example scope (policy data, not code) |
|------|--------------------------------------|
| `principal` | Everything within own tenant |
| `chief_of_staff` | Read all; execute actions up to configured autonomy tier |
| `delegate` | Read scoped projects; draft but not send |
| `observer` | Read-only briefs |

**Autonomy tiers** (OPA data per principal, per action type):

- `T0 draft-only` — CoS prepares, human sends
- `T1 confirm` — preflight allow + Go/No-Go card required
- `T2 auto` — execute and log (e.g., decline conflicting low-priority meetings)

Same policy bundle for every principal; only the data document differs. Eligible-approvers API becomes **eligible-actors**: "who can send email as this principal?" is a live OPA batch check over the ZITADEL directory.

## 5. CDC and indexing (read side)

**Kafka Connect** — eight MongoDB source connectors publish verbatim full documents:

| Kafka topic | Source collection |
|-------------|-------------------|
| `meetings` / `meeting_security_events` | meetings + its security events |
| `decisions` / `decision_security_events` | decisions + its security events |
| `commitments` / `commitment_security_events` | commitments + its security events |
| `actions` / `action_security_events` | actions + its security events |

**cos-indexer** — independent, self-contained Kafka consumers (full snapshots embedded, no API callbacks). Each pipeline writes:

1. **Neo4j graph** — `(Person)-[:ATTENDED]->(Meeting)`, `(Person)-[:OWES]->(Commitment)-[:FOR]->(Project)`, `(Decision)-[:DECIDED_BY]->(Person)`, `(Decision)-[:SUPERSEDES]->(Decision)`, `(Meeting)-[:PRODUCED]->(Decision|Commitment)`, `(Commitment)-[:BLOCKS]->(Project)` — all scoped by `(tenant {principalId})`.
2. **`MultimodalDocument` vector docs** with `source` ∈ `meeting_state`, `decision_state`, `commitment_state`, `action_security_event`, … in the `multimodal_embedding` index.

Denormalized fields on nodes for fast retrieval: `decided_at`, `decided_by`, `authorization_summary`, `due_at`, `slipped_count`.

## 6. Chat layer (`cos-chat`)

Spring AI + structured `RouterDecision` (Gemini Flash or any structured-output LLM — provider is config). One route call, then deterministic dispatch on `path`.

### Router paths

| Path | Handles | Backend |
|------|---------|---------|
| `skill` | Delegated mutations: send_email, move_meeting, assign_task, record_decision, create_commitment | OPA preflight → Go/No-Go → domain service POST |
| `me` | "What's on my plate / calendar today" | Deterministic calendar/commitment query + Thymeleaf formatter |
| `brief` | Meeting prep, daily brief | Scripted brief pipeline (graph pulls + synthesis template) |
| `eligibility` | "Who can approve/send/act as the principal?" | Live OPA eligible-actors |
| `audit` | "Who decided X / when / on what basis?" | `neo4j_direct` Cypher plans + formatters |
| `document_extraction` | Show meeting/decision/commitment by id, status, inventory lists | Domain GET (OBO) |
| `neo4j_direct` | Known graph shapes: blocked-on-whom, slipped commitments, decision history | In-process Cypher planner |
| `graph` / `vector` / `hybrid` | Open investigation ("what did we discuss about the reorg?") | Selective retrieval → LLM synthesis |

Open-vocabulary slots are LLM structured fields, never regex: "sometime next week" → `skillWindow`, "the platform migration call" → `meetingRef`, "paused projects" → `projectStatus=ON_HOLD`. Post-route clamps live in one documented `RouteClamps` class.

### Fast paths (no RAG)

- Today's calendar, open commitments, overdue items → direct query + formatter
- "Who can X?" → live OPA
- Counts, rankings, audit trails → Cypher plan + Thymeleaf

### Eligibility vs audit, CoS edition

| Question | Path |
|----------|------|
| "Who *can* send email as the principal?" | `eligibility` → OPA |
| "Who *sent* that email and under what authority?" | `audit` → graph security events |
| "Can I move the 3pm?" | OPA preflight on `skill` |
| "Why was the 3pm moved?" | `audit` |

## 7. Delegated-action skills

Scripted pipelines, one per action:

`send-email`, `draft-email`, `move-meeting`, `decline-meeting`, `assign-task`, `record-decision`, `create-commitment`, `nudge-owner` (follow-up on a slipped commitment).

Each skill: LLM slots from `RouterDecision` → validate → **OPA preflight** (autonomy tier for this principal + action) → **confirm card** (Go/No-Go, skipped only at T2) → POST to action-service → transactional record + security event → CDC → graph. The confirmation card is deterministic Thymeleaf, showing exactly what will happen and the policy basis.

## 8. Ingestion

Beyond direct user mutations, the system ingests the principal's work exhaust:

- **Connectors-in:** calendar (CalDAV/Graph/Google), email (IMAP/Graph), notes/transcripts (upload or API). Each connector is a thin adapter that POSTs to meeting-service — so ingested data flows through the same transaction + security-event + CDC path as everything else. No side doors into the graph.
- **Extraction workers:** transcript → action items / decisions / commitments (LLM structured output, same "strict schema, no fuzzy classification" rule), written back via domain services as PROPOSED records for human confirmation at T0/T1.

## 9. What v1 includes beyond the four capabilities

- **Daily brief** — scheduled scripted pipeline (graph pulls: today's meetings, overdue commitments, pending decisions, slipped items) + synthesis template. Stored as a versioned `Brief`.
- **Golden eval bank** — HTTP black-box YAML cases per router family (me / brief / eligibility / audit / skills), with a `prove.sh` runner. Non-negotiable: this is how routing changes are proven safe.
- **Observability** — routing metadata on every answer (path, strategy, cypher provenance, synthesis mode), Micrometer SLIs, `GET /api/routing-stats`, feedback endpoints.
- **Multi-tenancy** — `principalId` on every record, graph node, vector doc, and OPA input. One deployment serves many leaders.

## 10. Storage and topic names

| Layer | Name | Purpose |
|-------|------|---------|
| Vector index | `multimodal_embedding` | Dense search on `MultimodalDocument.embedding` |
| Vector `source` values | `meeting_state`, `decision_state`, `commitment_state`, `action_state`, `*_security_event`, `brief_state` | Chat mode filters |
| MongoDB | `cos_meetings.meetings`, `cos_decisions.decisions`, `cos_commitments.commitments`, `cos_actions.actions` | Versioned records |
| MongoDB | `security_events.{meeting,decision,commitment,action}_service` | Security events |
| Kafka | `meetings`, `decisions`, `commitments`, `actions` + `*_security_events` | Mongo CDC topics |
| MongoDB deployment | replica set `rs0` | Required for transactions + CDC (docker-compose init) |

## 11. Component map

```
connectors-in (calendar/email/notes)
        │ POST (OBO)
        ▼
┌─────────────────────────────────────────────┐
│ meeting-svc  decision-svc  commitment-svc   │──── authz-svc ──── OPA
│              action-svc                     │         │
└──────────────┬──────────────────────────────┘      ZITADEL
               │ single Mongo txn (record + security event)
               ▼
        MongoDB rs0 ──► Kafka Connect (CDC) ──► Kafka topics
                                                    │
                                                    ▼
                                              cos-indexer
                                             (8 pipelines)
                                                    │
                                     ┌──────────────┴─────────────┐
                                     ▼                            ▼
                                  Neo4j graph            multimodal_embedding
                                     ▲                            ▲
                                     └──────────┬─────────────────┘
                                                │ selective retrieval
                                            cos-chat
                              (RouterDecision → dispatch → skills /
                               fast paths / retrieval → synthesis)
                                                ▲
                                          principal / delegates
```

## 12. Build sequence

1. **Foundation** — docker-compose: Mongo rs0, Kafka + Connect, Neo4j, OPA, ZITADEL seed (`users.yaml` with generic roles). Shared libs: security-event schema, OBO client.
2. **Slice one entity end-to-end** — commitment-service → CDC → indexer pipeline → graph + vector → `me`/`audit` paths in cos-chat. Proves the whole spine with the simplest entity.
3. **Meetings + ingestion** — meeting-service, calendar connector, prep-brief pipeline.
4. **Decisions** — decision-service, decision graph, audit paths.
5. **Delegated actions** — action-service, OPA autonomy tiers, send-email + move-meeting skills with Go/No-Go.
6. **Daily brief + golden evals + observability.**

Each phase ends with golden eval cases for the paths it added.

