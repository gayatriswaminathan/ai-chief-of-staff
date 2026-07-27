# Platform — phase 1 foundation

The local spine of the AI Chief of Staff: MongoDB (replica set, transactional writes) → Kafka Connect CDC → Kafka topics, plus Neo4j (graph), OPA (policy), and optionally ZITADEL (identity).

See `../docs/architecture-spec.md` for the full design.

## Prerequisites

- Docker Desktop (Mac: https://www.docker.com/products/docker-desktop/ — Apple Silicon build), with ~8 GB RAM allocated (Settings → Resources).

## Run

```bash
cd platform
docker compose up -d          # mongo, kafka, connect, neo4j, opa
./scripts/register-connectors.sh   # wire Mongo CDC -> Kafka (wait until connect is healthy, ~1-2 min first run)
./scripts/verify.sh                # smoke-test the whole spine
```

`verify.sh` proves: replica set up, Kafka up, Connect up, Neo4j up, OPA allows/denies correctly per `opa/data.json`, and a Mongo insert flows through CDC onto the `commitments` Kafka topic.

Identity (phase 1b, not needed for the CDC spine):

```bash
docker compose --profile auth up -d    # ZITADEL console at http://localhost:8080 (admin / CosAdmin1!)
```

## Layout

| Path | Purpose |
|------|---------|
| `docker-compose.yml` | All infrastructure services |
| `opa/authz.rego` | Generic authorization rules (never leader-specific) |
| `opa/data.json` | Per-principal delegation data — the only place leaders/delegates are named |
| `connect/*.json` | Kafka Connect MongoDB source connector configs (one pair per entity) |
| `schema/security-event.json` | The audit event written with every mutation |
| `scripts/` | register-connectors, verify |

## Endpoints

| Service | URL | Credentials |
|---------|-----|-------------|
| MongoDB | mongodb://localhost:27017/?replicaSet=rs0 | none (local dev) |
| Kafka | localhost:9092 | none |
| Kafka Connect | http://localhost:8083 | none |
| Neo4j browser | http://localhost:7474 | neo4j / coslocal1 |
| OPA | http://localhost:8181 | none |
| ZITADEL (auth profile) | http://localhost:8080 | admin / CosAdmin1! |

## Next (phase 2)

commitment-service (Spring Boot: JWT → OPA preflight → record + security event in one txn) → cos-indexer (Kafka consumer → Neo4j + vectors) → cos-chat (`RouterDecision` → `me` / `audit` paths).
