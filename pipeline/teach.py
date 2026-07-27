"""The teaching loop — the SME's core operating model.
'Never do a task the old-school way. Every miss is a teaching moment in a
closed loop.' (Chawla, The Skip, Jun 2026)

Usage:
    python3 teach.py "Automated digests never appear as waiting-on-me items"

The correction is appended as a permanent, timestamped rule to
config/rules.md, logged in the audit chain, and injected into every future
extraction prompt. Rules are never silently dropped — removing one requires
an explicit retire command, which is itself audited.
"""
import os, sys
from datetime import date
sys.path.insert(0, os.path.dirname(__file__))
from audit import log

BASE = os.path.join(os.path.dirname(__file__), "..")
RULES = os.path.join(BASE, "config", "rules.md")

def teach(correction):
    n = 1
    if os.path.exists(RULES):
        n = sum(1 for l in open(RULES) if l.startswith("R")) + 1
    with open(RULES, "a") as f:
        f.write(f"R{n:03d} ({date.today().isoformat()}): {correction}\n")
    log("principal", "teach_rule", f"R{n:03d}: {correction}", "L1_approval_required")
    print(f"Rule R{n:03d} recorded. It applies to every run from now on.")

def retire(rule_id, reason):
    lines = open(RULES).readlines()
    with open(RULES, "w") as f:
        for l in lines:
            if l.startswith(rule_id):
                f.write(f"# RETIRED {date.today().isoformat()} ({reason}): {l}")
            else:
                f.write(l)
    log("principal", "retire_rule", f"{rule_id}: {reason}", "L1_approval_required")

if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--retire":
        retire(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 2:
        teach(sys.argv[1])
    else:
        print(__doc__)
