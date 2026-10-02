"""Score the target's real answers against the guardrail, and deliberately broken versions
of them, so a threshold can be chosen from the gap between the two.

    python scripts/guardrail.py apply guardrails/medibot.yaml --observe
    python scripts/measure_guardrail.py
    python scripts/guardrail.py apply guardrails/medibot.yaml       # back to enforcing

Runs in the observe posture, where nothing blocks and every score is returned. Measuring on
the guardrail that ships, rather than a copy, costs the two extra lines above.

The broken versions are built mechanically rather than written by hand: an answer paired
with a distant question's passages, an answer with every number changed, and an answer
checked against a different question. Writes `docs/measurements/scores.json`.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import unicodedata
from pathlib import Path

import boto3

REGION = "ap-south-1"
ANSWERS = Path("docs/measurements/answers.json")
OUT = Path("docs/measurements/scores.json")


def normalise(text: str) -> str:
    """Strip citation markers and fold unicode punctuation to ASCII.

    Without this, grounding scores the answer's typography: correct answers fell as low as
    0.19 on citation markers and a non-breaking hyphen alone.
    """
    return unicodedata.normalize("NFKC", re.sub(r"【[^】]*】", "", text))


REFERENCE_MARKER = re.compile(r"\[\d+\]")


def has_numeric_facts(text: str) -> bool:
    """Whether the answer states a number a reader would treat as a fact.

    Reference markers are pointers, not claims. An answer holding only those cannot be
    corrupted by changing its numbers, so scoring the result as a failure measures a
    correct answer.
    """
    return bool(re.search(r"\d", REFERENCE_MARKER.sub("", normalise(text))))


def mutate_numbers(text: str) -> str:
    """Every number changed, so the sentence still reads correctly and is no longer true."""
    return re.sub(r"\d+", lambda m: str(int(m.group()) + 3), text)


def apply(client, guardrail_id: str, *, source: str, blocks: list[dict]) -> dict:
    return client.apply_guardrail(
        guardrailIdentifier=guardrail_id,
        # DRAFT on purpose: measuring needs the observe posture, which `apply --observe`
        # writes to DRAFT. A published version is immutable and cannot be put into it.
        guardrailVersion="DRAFT",
        source=source,
        content=blocks,
    )


def grounding_scores(response: dict) -> dict[str, float]:
    """The score each contextual-grounding filter returned, by type."""
    scores: dict[str, float] = {}
    for assessment in response.get("assessments", []):
        for entry in assessment.get("contextualGroundingPolicy", {}).get("filters", []):
            scores[entry["type"]] = entry["score"]
    return scores


def detections(response: dict) -> list[str]:
    """Everything else that fired, as flat labels."""
    found = []
    for assessment in response.get("assessments", []):
        for entry in assessment.get("contentPolicy", {}).get("filters", []):
            found.append(f"content:{entry['type']}:{entry['confidence']}")
        for entry in assessment.get("topicPolicy", {}).get("topics", []):
            found.append(f"topic:{entry['name']}")
        sensitive = assessment.get("sensitiveInformationPolicy", {})
        for entry in sensitive.get("piiEntities", []):
            found.append(f"pii:{entry['type']}")
        for entry in sensitive.get("regexes", []):
            found.append(f"regex:{entry['name']}")
    return found


def output_blocks(question: str, answer: str, contexts: list[str]) -> list[dict]:
    return (
        [{"text": {"text": c, "qualifiers": ["grounding_source"]}} for c in contexts]
        + [{"text": {"text": question, "qualifiers": ["query"]}},
           {"text": {"text": answer, "qualifiers": ["guard_content"]}}]
    )


def summarise(label: str, values: list[float]) -> str:
    if not values:
        return f"  {label:22} (none)"
    return (f"  {label:22} n={len(values):3}  min={min(values):.3f}  "
            f"median={statistics.median(values):.3f}  max={max(values):.3f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--guardrail", default="gep-medibot")
    args = parser.parse_args()

    bedrock = boto3.client("bedrock", region_name=REGION)
    runtime = boto3.client("bedrock-runtime", region_name=REGION)

    identifier = None
    for page in bedrock.get_paginator("list_guardrails").paginate():
        for summary in page["guardrails"]:
            if summary["name"] == args.guardrail:
                identifier = summary["id"]
    if identifier is None:
        print(f"no guardrail named {args.guardrail}", file=sys.stderr)
        return 1
    print(f"measuring against {args.guardrail} ({identifier})\n")

    records = json.loads(ANSWERS.read_text())
    grounded = [r for r in records if r["contexts"] and not r["refusal"]]
    print(f"{len(grounded)} answers have passages to ground against\n")

    rows = []
    for index, record in enumerate(grounded):
        question, answer, contexts = record["question"], record["answer"], record["contexts"]
        # Furthest question, not the next one: the corpus is ordered by subject, so
        # neighbours share one and the pairing is not a mismatch at all.
        other = grounded[(index + len(grounded) // 2) % len(grounded)]

        cases = {
            "real": (question, answer, contexts),
            "real_normalised": (question, normalise(answer), contexts),
            "mismatched": (question, answer, other["contexts"]),
            **({"mutated": (question, mutate_numbers(normalise(answer)), contexts)}
               if has_numeric_facts(answer) else {}),
            "off_topic": (other["question"], answer, contexts),
        }
        for case, (q, a, ctx) in cases.items():
            response = apply(runtime, identifier, source="OUTPUT",
                             blocks=output_blocks(q, a, ctx))
            rows.append({
                "case": case,
                "role": record["role"],
                "question": question,
                **grounding_scores(response),
                "detections": detections(response),
            })
        print(f"  [{index + 1}/{len(grounded)}] {question[:58]}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, indent=2))

    print("\n--- contextual grounding, by case ---")
    for case in ("real", "real_normalised", "mismatched", "mutated", "off_topic"):
        for metric in ("GROUNDING", "RELEVANCE"):
            values = [r[metric] for r in rows if r["case"] == case and metric in r]
            print(summarise(f"{case}/{metric}", values))

    print("\n--- anything else that fired ---")
    fired = [(r["case"], r["question"][:44], d) for r in rows for d in r["detections"]]
    if not fired:
        print("  nothing: no content filter, topic or PII match on any answer")
    for case, question, detection in fired:
        print(f"  {case:11} {detection:28} {question}")

    raw = [r["GROUNDING"] for r in rows if r["case"] == "real" and "GROUNDING" in r]
    norm = [r["GROUNDING"] for r in rows if r["case"] == "real_normalised" and "GROUNDING" in r]
    bad = [r["GROUNDING"] for r in rows
           if r["case"] in ("mismatched", "mutated") and "GROUNDING" in r]
    print("\n--- the threshold argument, in one table ---")
    print(f"  worst correct answer, raw         {min(raw):.2f}")
    print(f"  worst correct answer, normalised  {min(norm):.2f}")
    print(f"  worst injected failure            {max(bad):.2f}")
    print(f"  => usable gap only after normalising: {max(bad):.2f} .. {min(norm):.2f}")

    print(f"\nwrote {OUT} — {len(rows)} measurements")
    return 0


if __name__ == "__main__":
    sys.exit(main())
