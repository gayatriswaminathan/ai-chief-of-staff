#!/usr/bin/env python3
"""Golden routing evals — proves the chat router still behaves after any change.

Usage:
  python3 eval/run_evals.py             # test whatever router is live (LLM if key set)
  python3 eval/run_evals.py --fallback  # skip llm_only cases (deterministic router only)
"""

import json
import sys
import urllib.request
from pathlib import Path

import yaml

CHAT = "http://localhost:8002/chat"
HEADERS = {"X-Actor": "cos@example.com", "X-Principal": "principal-001",
           "Content-Type": "application/json"}


def ask(question: str) -> dict:
    req = urllib.request.Request(CHAT, method="POST", headers=HEADERS,
                                 data=json.dumps({"question": question}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main() -> int:
    fallback_only = "--fallback" in sys.argv
    bank = yaml.safe_load((Path(__file__).parent / "golden.yaml").read_text())
    passed = failed = skipped = 0
    failures = []

    for family in bank:
        for case in family["cases"]:
            q = case["q"]
            if fallback_only and case.get("llm_only"):
                skipped += 1
                continue
            try:
                resp = ask(q)
            except Exception as exc:
                failed += 1
                failures.append((family["family"], q, f"request error: {exc}"))
                continue
            got_path = resp.get("routing", {}).get("path")
            want = case["path"]
            path_ok = (got_path in want.split("_or_")) if "_or_" in want else (got_path == want)
            content_ok = True
            if "answer_contains" in case:
                content_ok = case["answer_contains"] in resp.get("answer", "")
            if path_ok and content_ok:
                passed += 1
                print(f"PASS  [{family['family']}] {q}  -> {got_path}")
            else:
                failed += 1
                why = (f"path {got_path} != {want}" if not path_ok
                       else f"answer missing '{case['answer_contains']}'")
                failures.append((family["family"], q, why))
                print(f"FAIL  [{family['family']}] {q}  ({why})")

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    if failures:
        print("\nFailures:")
        for fam, q, why in failures:
            print(f"  [{fam}] {q}: {why}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
