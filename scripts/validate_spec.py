"""A structural checklist: does an artefact exist for each requirement.

    python scripts/validate_spec.py

This is a drift alarm, not evidence. Most rows assert that a file contains a thing — a
policy with the right filters, a report with the right sections, a README that names the
judge. Almost none of them execute the behaviour they are named after, and a row passing
means "there is something here", not "this works".

It earns its place by catching regressions: rewriting a README section once removed the
only mention of GROQ_API_KEY, and this is what noticed. It does not tell you whether a
report section was populated, whether a metric ran, or whether a test could fail.

For whether the thing actually works, read `docs/report.md`, `docs/measurements/` and the
test suite. Do not quote this script's score as a result.
"""

from __future__ import annotations

import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guardrail_eval_pipeline import heuristics, judge, ragas_metrics  # noqa: E402


def main() -> int:
    readme = (ROOT / "README.md").read_text()
    report = (ROOT / "docs" / "report.md").read_text()
    # The README orients; the detail lives in docs/. A requirement is satisfied by an
    # artefact anywhere in the documentation, so the prose checks read both.
    prose = readme + "\n".join(p.read_text() for p in sorted((ROOT / "docs").glob("*.md")))
    dataset = yaml.safe_load((ROOT / "evaluation" / "medibot.yaml").read_text())
    policy = yaml.safe_load((ROOT / "guardrails" / "medibot.yaml").read_text())
    source = lambda name: (ROOT / "src" / "guardrail_eval_pipeline" / name).read_text()

    checks = [
        # Role override is one named topic; off-topic is several concrete subjects rather
        # than one broad one, so this counts kinds of cover, not entries.
        ("1", "l.44 input guardrail: injection, off-topic, role override",
         any(t["name"] == "Unauthorized Access" for t in policy["topics"])
         and len([t for t in policy["topics"] if t["name"] != "Unauthorized Access"]) >= 1
         and all(t["type"] == "DENY" for t in policy["topics"])
         and any(f["type"] == "PROMPT_ATTACK" for f in policy["content_filters"])),
        ("1", "l.45 output guardrail: leaked content, PII, fabricated claims",
         bool(policy["pii_regexes"]) and any(g["type"] == "GROUNDING" for g in policy["grounding"])),
        ("1", "l.46 OpenEvals and/or Bedrock implements a layer", policy["name"] == "gep-medibot"),
        ("1", "l.47 structured verdicts, fail closed",
         source("guardrails/bedrock.py").count("failed_closed") >= 4),
        ("1", "l.48 reason logged, never echoed",
         "block_reason_is_not_leaked" in source("heuristics.py")),
        ("2", "l.58 LangSmith tracing of the full path", "@traceable" in source("service.py")),
        ("2", "l.59 every guardrail decision a structured event",
         (ROOT / "src/guardrail_eval_pipeline/events.py").is_file()),
        ("2", "l.60 latency and tokens per request, queryable",
         (ROOT / "scripts/metrics.py").is_file() and "trace_id" in source("events.py")),
        ("2", "l.61 one logged request explainable without re-running",
         "request_id" in source("events.py")),
        ("3", "l.69 at least 15 labelled pairs, normal and adversarial",
         len(dataset["cases"]) >= 15 and any(c["expect"] == "blocked" for c in dataset["cases"])),
        ("3", "l.70 the four named RAGAS metrics",
         set(ragas_metrics.METRICS) == {"faithfulness", "answer_relevancy",
                                        "context_precision", "context_recall"}),
        ("3", "l.71 a repeatable script, not a notebook",
         "--reuse" in (ROOT / "scripts/evaluate.py").read_text()),
        ("3", "l.72 per-question scores and an aggregate per metric",
         "RAGAS, per question" in report and "**aggregate**" in report),
        ("4", "l.80 a separate LLM call against an explicit rubric", "RUBRIC" in source("judge.py")),
        ("4", "l.81 structured score plus a written justification",
         len(judge.DIMENSIONS) == 4 and "comment" in judge.RUBRIC),
        ("4", "l.82 README names the judge and why not self-grading",
         judge.JUDGE_MODEL in prose and "blind spots" in prose),
        ("5", "l.92 at least 4 deterministic checks", len(heuristics.CHECKS) >= 4),
        ("5", "l.93 run in the same pipeline as Component 3",
         "heuristics" in source("runner.py")),
        ("6", "l.101 one report consolidating all four signals",
         all(s in report for s in ("Guardrail decisions", "Heuristic checks",
                                   "RAGAS, per question", "judge_mean"))),
        ("6", "l.102 a clear verdict, thresholds, named failures",
         "## Verdict:" in report and "Failed thresholds" in report),
        ("6", "l.103 an example of a block and of a heuristic failure",
         "correctly blocking an unsafe request" in report
         and "correctly failing a bad response" in report),
        ("S", "setup instructions including API keys",
         "uv sync --extra evaluation" in readme and "GROQ_API_KEY" in readme),
        ("S", "the target is named and linked", "github.com/LijiAlex/medibot" in readme),
        ("S", "at least 3 adversarial cases with real verdicts",
         prose.count("GUARDRAIL_INTERVENED") >= 3),
        ("S", "a sample report output",
         "docs/report.md" in readme and "## Verdict:" in report),
        ("S", "tool substitutions and why", "## Tool substitutions" in prose),
    ]

    gaps = [name for _, name, ok in checks if not ok]
    for component, name, ok in checks:
        print(f"  {'found  ' if ok else 'MISSING'} [{component}] {name}")
    print(f"\n{len(checks) - len(gaps)}/{len(checks)} requirements have an artefact present.")
    print("This is a structural check. It does not show that any of them work — "
          "see docs/report.md and the test suite for that.")
    if gaps:
        print("\ngaps:")
        for gap in gaps:
            print(f"  - {gap}")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
