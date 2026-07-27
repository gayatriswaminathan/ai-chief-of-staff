"""Extraction: LLM pass over normalized items -> structured JSON.
Produces decisions, commitments, items waiting on the principal, risks,
and a red/yellow/green status per project — every element carrying
source_ids pointing back to corpus items (hard rule: never_drop_sources).

With ANTHROPIC_API_KEY set, calls Claude with EXTRACT_PROMPT.
Without a key (demo mode), loads store/extraction.json — the cached
output of the same prompt run through Claude interactively.
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from audit import log

BASE = os.path.join(os.path.dirname(__file__), "..")

EXTRACT_PROMPT = """You are Attaché, chief-of-staff agent for {principal}.
Read the normalized items below. Extract, as JSON:
 decisions_made: [{{summary, project, decided_by, date, source_ids}}]
 waiting_on_principal: [{{ask, project, requested_by, deadline, why_it_matters, source_ids}}]
 commitments: [{{who, what, due, status(on_track|at_risk|blown), source_ids}}]
 risks_to_escalate: [{{risk, project, recommended_action, source_ids}}]
 projects: [{{name, status(red|yellow|green), one_line_reason, source_ids}}]
 vip_items: [{{from, item, source_ids}}]  # senders in the VIP list
Rules: every element MUST include source_ids. Rank waiting_on_principal by
impact on the goals config. Do not invent items. Do not summarize away
deadlines — preserve exact dates.
ITEMS: {items}
GOALS: {goals}
VIPS: {vips}"""

def run():
    with open(os.path.join(BASE, "store", "items.json")) as f:
        items = json.load(f)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        import urllib.request
        goals = open(os.path.join(BASE, "config", "goals.yaml")).read()
        vips = open(os.path.join(BASE, "config", "vip.yaml")).read()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({
                "model": "claude-sonnet-4-5",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": EXTRACT_PROMPT.format(
                    principal="Regina Chan", items=json.dumps(items),
                    goals=goals, vips=vips)}],
            }).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req).read())
        text = resp["content"][0]["text"]
        extraction = json.loads(text[text.find("{"):text.rfind("}") + 1])
        log("attache.extract", "llm_extraction",
            f"live API call, {len(items)} items in", "L3_autonomous")
    else:
        with open(os.path.join(BASE, "store", "extraction.json")) as f:
            extraction = json.load(f)
        log("attache.extract", "llm_extraction",
            f"demo mode: cached Claude extraction over {len(items)} items",
            "L3_autonomous")

    # HARD RULE ENFORCEMENT: never_drop_sources — reject any element without source_ids
    for section, rows in extraction.items():
        for row in rows:
            if not row.get("source_ids"):
                raise SystemExit(
                    f"HARD RULE VIOLATION never_drop_sources: {section}: {row}")
    log("attache.extract", "hard_rule_check",
        "never_drop_sources: PASS (all elements carry source_ids)",
        "L3_autonomous")

    with open(os.path.join(BASE, "store", "structured.json"), "w") as f:
        json.dump(extraction, f, indent=1)
    print("extraction complete:",
          {k: len(v) for k, v in extraction.items()})

if __name__ == "__main__":
    run()
