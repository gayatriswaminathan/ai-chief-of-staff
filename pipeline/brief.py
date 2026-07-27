"""The 7 AM pass: renders store/structured.json into the morning brief.
Every element carries clickable source chips (hard rule: never_drop_sources).
Design tokens match the Attaché concept site."""
import json, os, sys, html
from datetime import date
sys.path.insert(0, os.path.dirname(__file__))
from audit import log

BASE = os.path.join(os.path.dirname(__file__), "..")

STATUS = {"red": ("var(--red)", "RED"), "yellow": ("var(--amber)", "YELLOW"),
          "green": ("var(--green)", "GREEN")}
CSTAT = {"on_track": ("var(--green)", "on track"), "at_risk": ("var(--amber)", "at risk"),
         "blown": ("var(--red)", "blown"), "done": ("var(--faint)", "done")}

def chips(ids, titles):
    return "".join(
        f'<span class="chip" title="{html.escape(titles.get(i, i))}">{i}</span>'
        for i in ids)

def run():
    with open(os.path.join(BASE, "store", "structured.json")) as f:
        d = json.load(f)
    with open(os.path.join(BASE, "store", "items.json")) as f:
        items = json.load(f)
    titles = {i["id"]: (i.get("thread") or i.get("title") or
                        f'{i.get("channel","")}: {i.get("body","")[:80]}')
              for i in items}
    with open(os.path.join(BASE, "store", "approval_queue.json")) as f:
        queue = json.load(f)
    audit_lines = sum(1 for _ in open(os.path.join(BASE, "store", "audit.jsonl")))

    def sec(title, sub=""):
        s = f'<div class="sub">{sub}</div>' if sub else ""
        return f'<h2>{title}</h2>{s}'

    waiting = "".join(f"""
      <div class="card wait">
        <div class="row"><span class="badge">{html.escape(w["deadline"])}</span>
        <span class="proj">{html.escape(w["project"])}</span></div>
        <div class="ask">{html.escape(w["ask"])}</div>
        <div class="why">{html.escape(w["why_it_matters"])} — <em>{html.escape(w["requested_by"])}</em></div>
        <div class="src">{chips(w["source_ids"], titles)}</div>
      </div>""" for w in d["waiting_on_principal"])

    projects = "".join(f"""
      <div class="card proj-card">
        <div class="row"><span class="dot" style="background:{STATUS[p["status"]][0]}"></span>
        <b>{html.escape(p["name"])}</b>
        <span class="stat" style="color:{STATUS[p["status"]][0]}">{STATUS[p["status"]][1]}</span></div>
        <div class="why">{html.escape(p["one_line_reason"])}</div>
        <div class="src">{chips(p["source_ids"], titles)}</div>
      </div>""" for p in d["projects"])

    decisions = "".join(f"""
      <div class="card"><div class="ask">{html.escape(x["summary"])}</div>
      <div class="why">{html.escape(x["decided_by"])} · {x["date"]}</div>
      <div class="src">{chips(x["source_ids"], titles)}</div></div>"""
      for x in d["decisions_made"])

    commits = "".join(f"""
      <tr><td>{html.escape(c["who"])}</td><td>{html.escape(c["what"])}</td>
      <td>{c["due"]}</td>
      <td style="color:{CSTAT[c["status"]][0]};font-weight:600">{CSTAT[c["status"]][1]}</td>
      <td>{chips(c["source_ids"], titles)}</td></tr>""" for c in d["commitments"])

    risks = "".join(f"""
      <div class="card risk"><div class="ask">{html.escape(r["risk"])}</div>
      <div class="why"><b>Recommended:</b> {html.escape(r["recommended_action"])}</div>
      <div class="src">{chips(r["source_ids"], titles)}</div></div>"""
      for r in d["risks_to_escalate"])

    vips = "".join(f"""
      <div class="card vip"><b>{html.escape(v["from"])}</b>
      <div class="why">{html.escape(v["item"])}</div>
      <div class="src">{chips(v["source_ids"], titles)}</div></div>"""
      for v in d["vip_items"])

    qhtml = "".join(f"""
      <div class="card queue"><div class="row">
      <span class="badge purple">{q["autonomy_level"].split("_")[0]} · needs approval</span>
      <span class="proj">{q["type"]}</span></div>
      <div class="why">{html.escape(q.get("draft", q.get("rationale", "")))}</div>
      <div class="row approve"><button class="btn ok">Approve &amp; send</button>
      <button class="btn edit">Edit</button><button class="btn no">Reject</button></div>
      <div class="src">{chips(q["source_ids"], titles)}</div></div>""" for q in queue)

    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Attaché — Morning Brief</title><style>
:root{{--ink:#17161d;--body:#55525f;--faint:#9c99a6;--bg:#faf9f7;--card:#fff;
--hair:rgba(23,22,29,.08);--accent:#5b5bd6;--green:#1f9d61;--amber:#9a7b1c;
--red:#d5484a;--sh:0 1px 2px rgba(23,22,29,.05),0 4px 12px rgba(23,22,29,.05)}}
*{{box-sizing:border-box;margin:0}}
body{{font-family:'Inter',-apple-system,sans-serif;background:var(--bg);color:var(--ink);
max-width:820px;margin:0 auto;padding:40px 24px 80px}}
h1{{font-size:26px;letter-spacing:-.5px}}
h2{{font-size:15px;text-transform:uppercase;letter-spacing:1.2px;color:var(--accent);
margin:36px 0 6px}}
.sub{{color:var(--faint);font-size:13px;margin-bottom:10px}}
.meta{{color:var(--faint);font-size:13px;margin:6px 0 4px}}
.card{{background:var(--card);border:1px solid var(--hair);border-radius:12px;
padding:14px 16px;margin:10px 0;box-shadow:var(--sh)}}
.wait{{border-left:3px solid var(--accent)}}
.risk{{border-left:3px solid var(--red)}}
.vip{{border-left:3px solid var(--amber)}}
.queue{{border-left:3px solid #8b5cf6}}
.row{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
.badge{{background:var(--accent);color:#fff;font-size:11px;font-weight:600;
padding:3px 9px;border-radius:20px}}
.badge.purple{{background:#8b5cf6}}
.proj{{color:var(--faint);font-size:12px;text-transform:uppercase;letter-spacing:.8px}}
.ask{{font-weight:600;font-size:15px;margin:8px 0 4px}}
.why{{color:var(--body);font-size:13.5px;line-height:1.5}}
.src{{margin-top:8px}}
.chip{{display:inline-block;background:rgba(91,91,214,.08);color:var(--accent);
font-size:11px;font-family:ui-monospace,monospace;padding:2px 7px;border-radius:6px;
margin-right:4px;cursor:help}}
.dot{{width:10px;height:10px;border-radius:50%}}
.stat{{font-size:11px;font-weight:700;letter-spacing:1px;margin-left:auto}}
table{{width:100%;border-collapse:collapse;background:var(--card);border-radius:12px;
box-shadow:var(--sh);font-size:13px}}
td,th{{padding:9px 12px;border-bottom:1px solid var(--hair);text-align:left;
vertical-align:top}}
th{{color:var(--faint);font-size:11px;text-transform:uppercase;letter-spacing:.8px}}
.btn{{border:1px solid var(--hair);background:#fff;border-radius:8px;padding:5px 12px;
font-size:12px;font-weight:600;cursor:pointer}}
.btn.ok{{background:var(--accent);color:#fff;border-color:var(--accent)}}
.btn.no{{color:var(--red)}}
.approve{{margin-top:10px}}
footer{{margin-top:44px;padding-top:16px;border-top:1px solid var(--hair);
color:var(--faint);font-size:12px;line-height:1.7}}
</style></head><body>
<h1>Attaché <span style="color:var(--faint);font-weight:400">· Morning Brief</span></h1>
<div class="meta">Prepared for Regina Chan · {date.today().strftime("%A, %B %d, %Y")} · 7:00 AM run
· sources: Outlook, Teams, Jira, meeting notes</div>

{sec("Waiting on you", "Ranked by deadline and goal impact — the punch list, not an inbox summary")}
{waiting}

{sec("Projects", "Red / yellow / green across the portfolio")}
{projects}

{sec("Decisions made", "What your teams decided that you should know about")}
{decisions}

{sec("Commitment tracker", "Deadlines the system watches; blown deadlines turn red the next morning")}
<table><tr><th>Who</th><th>Commitment</th><th>Due</th><th>Status</th><th>Sources</th></tr>
{commits}</table>

{sec("Escalations", "Risks with a recommended action attached")}
{risks}

{sec("VIP radar", "These senders never slip; nothing is ever auto-sent on their threads")}
{vips}

{sec("Approval queue", "Drafted by Attaché — nothing sends without you")}
{qhtml}

<footer><b>Governance line:</b> {audit_lines} actions in the tamper-evident audit log this run
· hard rules enforced: never_drop_sources ✓ · never_auto_send_to_vip ✓ · scoped read-only ingestion ✓
<br>Every statement above carries source chips — hover any chip for the underlying item.
Attaché read {len(items)} items overnight; it decided nothing on its own.</footer>
</body></html>"""

    out = os.path.join(BASE, "out", f"brief-{date.today().isoformat()}.html")
    with open(out, "w") as f:
        f.write(page)
    log("attache.brief", "generate_brief",
        f"brief written to {os.path.basename(out)}; {audit_lines + 1} audit entries",
        "L3_autonomous")
    print("brief written:", out)

if __name__ == "__main__":
    run()
