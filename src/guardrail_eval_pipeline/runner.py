"""Run the evaluation set through the guarded path and collect every signal.

Order matters and is the spec's own advice: deterministic checks first, because they cost
nothing, so a broken system fails before anything is spent on RAGAS or the judge.

The runner calls `service.handle` directly rather than the HTTP endpoint. Both share the
guarded path, so the evaluation cannot accidentally measure an unguarded system — and it
authenticates from configuration, since it has no caller's token of its own.

Answers are saved. Re-scoring a saved run is how "running it twice produces consistent
results" is achievable at all: the target is a language model and will not repeat itself
word for word, so repeatability belongs to the scoring, not to the generation.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from guardrail_eval_pipeline import heuristics, judge as judging, ragas_metrics, service
from guardrail_eval_pipeline.adapters import build_target
from guardrail_eval_pipeline.config import TargetConfig, load_policy, load_target
from guardrail_eval_pipeline.contracts import Retrieved, TargetError, TargetResponse
from guardrail_eval_pipeline.dataset import Case, Dataset, load as load_dataset
from guardrail_eval_pipeline.guardrails import BedrockGuardrail


@dataclass
class CaseResult:
    case_id: str
    question: str
    principal: str
    expected: str
    observed: str
    answer: str
    citations: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    checks: list[dict[str, str]] = field(default_factory=list)
    ragas: dict[str, Any] = field(default_factory=dict)
    judge: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class Run:
    target: str
    cases: list[CaseResult] = field(default_factory=list)
    probes: list[dict[str, Any]] = field(default_factory=list)
    ragas_aggregate: dict[str, float | None] = field(default_factory=dict)
    ragas_coverage: dict[str, list[int]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # Stamped when RAGAS runs. A report rebuilt later reads this rather than the current
    # environment, so it cannot attribute scores to a model that never saw them.
    evaluator: dict[str, str] = field(default_factory=dict)


def _answer(case, target, guardrail) -> tuple[heuristics.Outcome, str | None]:
    """Ask one question through the guarded path, timing it.

    The timing is the runner's own stopwatch, not the service's. A latency threshold is one
    of the spec's heuristic examples, and the measurement belongs to whoever is making the
    claim about it.
    """
    started = time.perf_counter()
    try:
        result = service.handle(case.question, target=target,
                                principal=case.principal, guardrails=guardrail)
        error = None
    except TargetError as exc:
        # The target being down is not a failing answer: it is an absent one, and the
        # report must not read it as the system behaving badly.
        result = service.Guarded(response=TargetResponse(answer=""), blocked=False)
        error = f"{type(exc).__name__}: {exc}"
    elapsed = (time.perf_counter() - started) * 1000

    outcome = heuristics.Outcome(
        case=case, response=result.response, blocked=result.blocked,
        reasons=tuple(result.verdict.reasons) if result.verdict else (),
        elapsed_ms=elapsed,
    )
    return outcome, error


def collect(dataset: Dataset, config: TargetConfig, *, guardrail=None,
            pace_s: float = 0.0) -> Run:
    """Run every case and apply the deterministic checks. No model calls beyond the target."""
    target = build_target(config)
    run = Run(target=dataset.target)

    for case in dataset.cases:
        outcome, error = _answer(case, target, guardrail)
        checks = heuristics.run(outcome)
        run.cases.append(CaseResult(
            case_id=case.id, question=case.question, principal=case.principal,
            expected=case.expect, observed=outcome.observed,
            answer=outcome.response.answer,
            citations=list(outcome.response.citations),
            contexts=[c.text for c in outcome.response.contexts],
            reasons=list(outcome.reasons), elapsed_ms=round(outcome.elapsed_ms, 1),
            checks=[asdict(c) for c in checks], error=error,
        ))
        if pace_s:
            # The target's provider caps tokens per minute; without a gap a full set
            # spends the run backing off instead of answering.
            time.sleep(pace_s)

    close = getattr(target, "close", None)
    if close is not None:
        close()
    return run


def _outcomes(dataset: Dataset, run: Run) -> list[heuristics.Outcome]:
    """Rebuild outcomes from a run, so saved results can be re-scored without re-asking."""
    by_id = {case.id: case for case in dataset.cases}
    unknown = [r.case_id for r in run.cases if r.case_id not in by_id]
    if unknown:
        # The saved run predates a change to the set. Re-scoring it would compare answers
        # against labels that no longer describe them, which is worse than refusing.
        raise ValueError(
            f"saved run has cases the evaluation set no longer defines: {unknown}. "
            "Run without --reuse to ask the target again."
        )
    rebuilt = []
    for result in run.cases:
        rebuilt.append(heuristics.Outcome(
            case=by_id[result.case_id],
            response=TargetResponse(
                answer=result.answer,
                contexts=[Retrieved(text=t) for t in result.contexts],
                citations=list(result.citations),
                refused=result.observed == "target_refused",
            ),
            blocked=result.observed == "blocked",
            reasons=tuple(result.reasons), elapsed_ms=result.elapsed_ms,
        ))
    return rebuilt


def rescore(dataset: Dataset, run: Run) -> None:
    """Re-apply the deterministic checks to saved answers.

    Without this, `--reuse` scores against whatever checks were stored, so fixing a label
    or a check changes nothing until the target is asked all over again — which defeats
    the point of saving the answers.
    """
    for result, rebuilt in zip(run.cases, _outcomes(dataset, run)):
        result.checks = [asdict(check) for check in heuristics.run(rebuilt)]


def add_ragas(dataset: Dataset, run: Run) -> None:
    outcomes = _outcomes(dataset, run)
    scored = ragas_metrics.score(outcomes)
    for result, score in zip(run.cases, scored):
        result.ragas = ({"unavailable": score.unavailable} if not score.usable
                        else {k: v for k, v in score.scores.items()})
    run.ragas_aggregate = ragas_metrics.aggregate(scored)
    run.ragas_coverage = {k: list(v) for k, v in ragas_metrics.coverage(scored).items()}
    run.evaluator = ragas_metrics.active_evaluator()


def add_judge(dataset: Dataset, run: Run, *, pace_s: float = 0.0) -> None:
    """Grade every answered or refused case, and the probes.

    Blocked cases are not graded: the target never answered, so the text is the pipeline's
    own refusal and grading it would measure this project's wording.
    """
    by_id = {case.id: case for case in dataset.cases}
    for result in run.cases:
        case = by_id[result.case_id]
        if result.observed == "blocked":
            result.judge = {"skipped": "blocked before the target answered"}
            continue
        verdict = judging.judge(
            result.question, result.answer, reference=case.expected_answer,
            contexts=result.contexts, expected_behaviour=case.expect,
        )
        result.judge = ({"unavailable": verdict.error} if not verdict.usable else
                        {**verdict.scores, "mean": round(verdict.mean, 3),
                         "comment": verdict.comment, "model": verdict.model})
        if pace_s:
            time.sleep(pace_s)

    # Replaced, not appended. Re-scoring a saved run twice was doubling this list, which
    # inflated the probe count and made the pass rate describe a set that did not exist.
    run.probes = []
    for probe in dataset.probes:
        verdict = judging.judge(probe.question, probe.answer, reference=None,
                                contexts=list(probe.contexts),
                                expected_behaviour="answered")
        # The deterministic checks run over the probes too. A probe is a fixed bad answer
        # with the passages it should have been built from, which is exactly what a
        # containment check needs — and it demonstrates a heuristic catching a bad response
        # without waiting for the target to produce one.
        probe_outcome = heuristics.Outcome(
            case=Case(id=probe.id, question=probe.question, principal="n/a",
                      expect="answered", expected_answer="(probe)"),
            response=TargetResponse(answer=probe.answer,
                                    contexts=[Retrieved(text=c) for c in probe.contexts],
                                    citations=[]),
            blocked=False, reasons=(), elapsed_ms=0.0)
        probe_checks = [asdict(c) for c in heuristics.run(probe_outcome)]

        entry = {"id": probe.id, "expect_judge": probe.expect_judge, "notes": probe.notes,
                 "answer": probe.answer, "checks": probe_checks}
        if not verdict.usable:
            entry["unavailable"] = verdict.error
        else:
            # The probe passes when the judge agreed with the label: a wrong answer should
            # score low, a correct refusal should not. A judge that praises everything
            # fails here, which is the point of having probes at all.
            entry.update(mean=round(verdict.mean, 3), comment=verdict.comment,
                         caught=(verdict.mean < 0.6) == (probe.expect_judge == "fail"))
        run.probes.append(entry)
        if pace_s:
            time.sleep(pace_s)


def save(run: Run, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(run), indent=2, ensure_ascii=False))
    return path


def load_run(path: Path) -> Run:
    data = json.loads(Path(path).read_text())
    run = Run(target=data["target"], ragas_aggregate=data.get("ragas_aggregate", {}),
              ragas_coverage=data.get("ragas_coverage", {}),
              notes=data.get("notes", []), probes=data.get("probes", []),
              evaluator=data.get("evaluator", {}))
    run.cases = [CaseResult(**case) for case in data["cases"]]
    return run


def build_guardrail(config: TargetConfig, repo_root: Path):
    """The guardrail this target configures, or None when it configures none."""
    policy_path = (config.raw.get("guardrails") or {}).get("policy")
    if not policy_path:
        return None
    return BedrockGuardrail.from_policy(load_policy(repo_root / policy_path))


__all__ = ["collect", "rescore", "add_ragas", "add_judge", "save", "load_run", "build_guardrail",
           "Run", "CaseResult", "load_dataset", "load_target"]
