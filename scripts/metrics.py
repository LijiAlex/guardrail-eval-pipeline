"""Count what the guardrails have been deciding.

    python scripts/metrics.py                 # the wired-in target
    python scripts/metrics.py --target stub

The spec asks for structured, queryable events and says a log line is acceptable and a
dashboard is not required. This is the query: it reads `logs/<target>/events.jsonl` and
aggregates it, and it is what the evaluation report reads for its block and allow counts.
"""

from __future__ import annotations

import argparse
from collections import Counter

from guardrail_eval_pipeline import events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="medibot")
    args = parser.parse_args()

    rows = events.read(args.target)
    if not rows:
        print(f"no events for {args.target!r} at {events.path_for(args.target)}")
        return

    decisions = Counter(row["decision"] for row in rows)
    stages = Counter(row["blocked_at"] for row in rows if row["blocked_at"])
    reasons = Counter(reason for row in rows for reason in row["reasons"])
    failures = sum(1 for row in rows if row["failed_closed"])

    print(f"{len(rows)} requests  ({rows[0]['at'][:19]} .. {rows[-1]['at'][:19]})\n")
    # Four outcomes, deliberately not three. A target refusing on its own terms is it
    # working; folding that into "blocked" would report correct behaviour as an attack.
    for name in ("allowed", "masked", "target_refused", "blocked"):
        count = decisions.get(name, 0)
        share = 100 * count / len(rows)
        print(f"  {name:16} {count:4}  {share:5.1f}%")

    if failures:
        # Separated because both stop a request and only one says anything about the text.
        print(f"\n  of which failed closed: {failures} "
              f"(the guardrail could not decide, so the request was refused)")
    if stages:
        print("\nblocked at:", ", ".join(f"{k} {v}" for k, v in stages.most_common()))
    if reasons:
        print("\npolicies that fired")
        for reason, count in reasons.most_common():
            print(f"  {reason:28} {count}")

    # Spec l.60: latency and token usage per request, exposed as basic metrics.
    latencies = sorted(r["latency_ms"] for r in rows if r.get("latency_ms") is not None)
    if latencies:
        middle = latencies[len(latencies) // 2]
        print(f"\nlatency  median {middle:,.0f}ms   min {latencies[0]:,.0f}ms   "
              f"max {latencies[-1]:,.0f}ms   ({len(latencies)} requests)")
    units = {}
    for row in rows:
        for name, count in (row.get("usage") or {}).items():
            units[name] = units.get(name, 0) + count
    if any(units.values()):
        print("guardrail units consumed: "
              + ", ".join(f"{k} {v}" for k, v in sorted(units.items()) if v))

    versions = {row["guardrail"].get("version") for row in rows if row.get("guardrail")}
    if versions - {None, ""}:
        print(f"\nguardrail versions seen: {', '.join(sorted(v for v in versions if v))}")


if __name__ == "__main__":
    main()
