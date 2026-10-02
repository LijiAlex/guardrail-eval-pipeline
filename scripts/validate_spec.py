"""Check every assignment requirement against something real in this repository.

    python scripts/validate_spec.py

Each row asserts an artefact exists and says what it is — a policy with the right filters,
a report with the right sections, a README with the right evidence. It is a guard against
a requirement being quietly satisfied by a sentence rather than by code.

What it cannot tell you is whether a section was *populated*: RAGAS and judge scores depend
on a provider key, and a report with those marked unavailable still passes the structural
check. The report itself is honest about which it is.
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
    dataset = yaml.safe_load((ROOT / "evaluation" / "medibot.yaml").read_text())
    policy = yaml.safe_load((ROOT / "guardrails" / "medibot.yaml").read_text())
    source = lambda name: (ROOT / "src" / "guardrail_eval_pipeline" / name).read_text()

    checks = [
        ("1", "l.44 input guardrail: injection, off-topic, role override",
         len(policy["topics"]) == 2
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
         judge.JUDGE_MODEL in readme and "blind spots" in readme),
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
         readme.count("GUARDRAIL_INTERVENED") >= 3),
        ("S", "a sample report output", "## Sample report" in readme),
        ("S", "tool substitutions and why", "## Tool substitutions" in readme),
    ]

    gaps = [name for _, name, ok in checks if not ok]
    for component, name, ok in checks:
        print(f"  {'PASS' if ok else 'GAP '}  [{component}] {name}")
    print(f"\n{len(checks) - len(gaps)}/{len(checks)} requirements evidenced")
    if gaps:
        print("\ngaps:")
        for gap in gaps:
            print(f"  - {gap}")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
