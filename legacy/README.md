# Attaché — a governance-native AI Chief of Staff

A working pipeline, not a mockup: overnight ingestion → LLM extraction to
structured JSON with source pointers → tamper-evident audit log → human
approval queue → 7 AM morning brief.

## Run it

```
python3 pipeline/ingest.py    # 2 AM pass: read sources, normalize 32 items
python3 pipeline/extract.py   # LLM pass: decisions, commitments, risks, RYG
python3 pipeline/brief.py     # 7 AM pass: render out/brief-YYYY-MM-DD.html
```

No API key needed for the demo — `extract.py` uses a cached Claude extraction.
Set `ANTHROPIC_API_KEY` and the same prompt runs live.

## What's demonstrated

- **The punch list, not an inbox summary** — decisions made, decisions waiting
  on the principal (deadline-ranked), red/yellow/green per project
  (pattern: Jagjit Chawla, VP Product, Meta — The Skip, Jun 2026)
- **Never drop sources** — every element must carry `source_ids`; extract.py
  hard-fails otherwise (verified by negative test)
- **Tamper-evident audit log** — every agent action hash-chained in
  `store/audit.jsonl` (pattern: JPMC "Securing the next generation of AI
  agents," Mar 2026)
- **Graduated autonomy** — `config/autonomy.yaml`: L1 approval-required →
  L3 autonomous, with graduation and trust-decay policy
  (pattern: De Jesus, 2026)
- **VIP never-auto-send** — `config/vip.yaml`; nothing sends without approval,
  ever, for listed senders
- **Goals-aware ranking** — `config/goals.yaml` weights what surfaces first
  (pattern: Murchison, claude-chief-of-staff)
- **Approval queue** — Attaché drafts replies, ticket assignments, calendar
  holds; a human presses send. The agent decided nothing on its own.

## Data

`corpus/` is a synthetic, fictional banking portfolio (5 projects, 32 items
across Outlook, Teams, Jira, meeting notes) — no real data. In production,
ingestion swaps to Microsoft Graph (delegated Mail.Read / Calendars.Read /
Chat.Read, or application permissions constrained by Exchange application
access policies), Jira REST, and the firm's sanctioned LLM gateway as the
model layer. The pipeline is source-agnostic past `store/items.json`.

## Layout

```
config/    vip.yaml · goals.yaml · autonomy.yaml (hard rules live here)
corpus/    synthetic source data (swap for real connectors)
pipeline/  ingest.py · extract.py · brief.py · audit.py
store/     items.json · structured.json · approval_queue.json · audit.jsonl
out/       brief-YYYY-MM-DD.html — open this
```
