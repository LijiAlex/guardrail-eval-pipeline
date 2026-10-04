"""Run the evaluation pipeline end to end.

    python scripts/evaluate.py                     # ask the target, score, report
    python scripts/evaluate.py --reuse runs/latest.json   # re-score saved answers
    python scripts/evaluate.py --skip-judge --skip-ragas  # heuristics only, no model calls

A script and not a notebook, because the spec asks for something repeatable. The target is
a language model and will not repeat itself word for word, so `--reuse` is how running
twice gives identical results: the answers are saved, and the scoring is deterministic
over them.

Deterministic checks run first. They cost nothing, so a broken system should fail before
anything is spent on RAGAS or the judge.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import guardrail_eval_pipeline  # noqa: F401  — loads .env
from guardrail_eval_pipeline import report, runner

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", default="evaluation/medibot.yaml")
    parser.add_argument("--target", default="targets/medibot.yaml")
    parser.add_argument("--out", default="docs/report.md")
    parser.add_argument("--run-file", default="runs/latest.json")
    parser.add_argument("--reuse", help="score a saved run instead of asking the target")
    parser.add_argument("--skip-ragas", action="store_true")
    parser.add_argument("--skip-judge", action="store_true")
    parser.add_argument("--pace", type=float, default=6.0,
                        help="seconds between model calls, for the provider's rate limit")
    args = parser.parse_args()

    dataset = runner.load_dataset(ROOT / args.set)
    config = runner.load_target(ROOT / args.target)

    if args.reuse:
        run = runner.load_run(Path(args.reuse))
        print(f"re-scoring {len(run.cases)} saved answers from {args.reuse}")
    else:
        guardrail = runner.build_guardrail(config, ROOT)
        print(f"asking the target {len(dataset.cases)} questions "
              f"({'guarded' if guardrail else 'UNGUARDED — no guardrail configured'})")
        run = runner.collect(dataset, config, guardrail=guardrail, pace_s=args.pace)
        runner.save(run, Path(args.run_file))
        print(f"  saved to {args.run_file}")

    # Deterministic first: if these fail the system is broken and the rest is spending.
    # Re-applied even on a reused run, so a corrected label or check takes effect without
    # asking the target anything.
    runner.rescore(dataset, run)
    failures = [c for c in run.cases
                for check in c.checks if check["status"] == "fail"]
    print(f"\nheuristics: {len(failures)} failing check(s) across {len(run.cases)} cases")

    if not args.skip_ragas:
        print("running RAGAS ...")
        runner.add_ragas(dataset, run)
    if not args.skip_judge:
        print("running the judge ...")
        runner.add_judge(dataset, run, pace_s=args.pace)

    runner.save(run, Path(args.run_file))
    text = report.build(run, dataset)
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)

    # The brief requires a sample report in the README; generating it from this same
    # report is what keeps the two from disagreeing.
    if report.update_readme(ROOT / "README.md", text):
        print("README sample block refreshed from this report")

    verdict = next(line for line in text.splitlines() if line.startswith("## Verdict"))
    print(f"\n{verdict}\nreport written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
