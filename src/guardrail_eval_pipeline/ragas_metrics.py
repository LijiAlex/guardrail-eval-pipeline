"""The four RAGAS metrics the spec names, over the cases that can carry them.

    faithfulness        is the answer supported by the passages
    answer relevancy    does it address the question
    context precision   were the retrieved passages the useful ones
    context recall      did retrieval find what the reference answer needed

Three of those need passages, so a case the target answered from records has nothing for
them to measure. Those are reported UNAVAILABLE. A zero would be indistinguishable from a
system that retrieved badly, and only one of those is a fault.

The evaluator runs on `openai/gpt-oss-20b` and not the target's `gpt-oss-120b`: the
provider's quota is per model as well as per organisation, so a separate model is both a
separation of concerns and a separate budget. Embeddings come from Bedrock, which spends
no provider quota at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Only cases that produced an answer AND the passages behind it can be scored.
METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")

EVALUATOR_MODEL = "openai/gpt-oss-20b"
EMBEDDING_MODEL = "cohere.embed-english-v3"
EMBEDDING_REGION = "ap-south-1"


@dataclass
class Scored:
    """RAGAS output for one case, or the reason there is none."""

    case_id: str
    scores: dict[str, float | None] = field(default_factory=dict)
    unavailable: str | None = None

    @property
    def usable(self) -> bool:
        return self.unavailable is None


def eligible(outcome) -> str | None:
    """Why this outcome cannot be scored, or None when it can.

    Stated as a reason rather than a boolean so the report can say which cases were left
    out and why, instead of quietly running over a smaller set.
    """
    if outcome.observed != "answered":
        return f"{outcome.observed}: no answer to score"
    if not outcome.response.contexts:
        return "answered from records, so there are no passages to measure against"
    if not outcome.case.expected_answer:
        return "no reference answer"
    return None


def _evaluator():
    """The LLM and embeddings RAGAS will use, wrapped for its interface."""
    from langchain_aws import BedrockEmbeddings
    from langchain_groq import ChatGroq
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper

    if not os.environ.get("GROQ_API_KEY"):
        raise RuntimeError("GROQ_API_KEY is not set; RAGAS cannot run")
    # Temperature zero: the spec asks that running twice against an unchanged system give
    # consistent results, and these metrics are themselves model calls.
    #
    # A generous timeout and retries because the provider rate-limits: the first run lost
    # most of its scores to TimeoutError, and a metric that silently drops cases is worse
    # than one that takes longer.
    llm = ChatGroq(model=EVALUATOR_MODEL, temperature=0, timeout=120, max_retries=5)
    embeddings = BedrockEmbeddings(model_id=EMBEDDING_MODEL, region_name=EMBEDDING_REGION)
    return LangchainLLMWrapper(llm), LangchainEmbeddingsWrapper(embeddings)


def score(outcomes) -> list[Scored]:
    """Score every eligible outcome. Ineligible ones come back marked, not dropped."""
    results = [Scored(case_id=o.case.id, unavailable=eligible(o)) for o in outcomes]
    scorable = [(o, r) for o, r in zip(outcomes, results) if r.usable]
    if not scorable:
        return results

    try:
        llm, embeddings = _evaluator()
        from ragas import evaluate
        from ragas.dataset_schema import SingleTurnSample
        from ragas.metrics import (answer_relevancy, context_precision, context_recall,
                                   faithfulness)
        from ragas import EvaluationDataset

        dataset = EvaluationDataset(samples=[
            SingleTurnSample(
                user_input=o.case.question,
                response=o.response.answer,
                retrieved_contexts=[c.text for c in o.response.contexts],
                reference=o.case.expected_answer,
            )
            for o, _ in scorable
        ])
        from ragas.run_config import RunConfig

        # answer_relevancy generates `strictness` paraphrases of the question in one call,
        # which it does by asking for n completions. Groq accepts n=1 only and rejects the
        # rest with a 400, so every case using it failed. One paraphrase, and the metric
        # runs.
        answer_relevancy.strictness = 1

        frame = evaluate(
            dataset,
            metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
            llm=llm, embeddings=embeddings,
            # Two at a time. The default fans out far enough to trip the rate limit, and a
            # throttled job comes back as a missing score rather than an error.
            run_config=RunConfig(max_workers=2, timeout=180, max_retries=5),
        ).to_pandas()
    except Exception as exc:  # noqa: BLE001
        # One failure marks every scorable case, rather than leaving some scored and some
        # not, which would make an aggregate silently cover a different set each run.
        for _, result in scorable:
            result.unavailable = f"RAGAS did not run: {type(exc).__name__}: {exc}"
        return results

    for index, (_, result) in enumerate(scorable):
        row = frame.iloc[index]
        for metric in METRICS:
            value = row.get(metric)
            result.scores[metric] = None if value is None or value != value else float(value)
    return results


def aggregate(results: list[Scored]) -> dict[str, float | None]:
    """Mean per metric over the cases that produced one. None where none did."""
    summary: dict[str, float | None] = {}
    for metric in METRICS:
        values = [r.scores.get(metric) for r in results if r.usable]
        values = [v for v in values if v is not None]
        summary[metric] = sum(values) / len(values) if values else None
    return summary


def coverage(results: list[Scored]) -> dict[str, tuple[int, int]]:
    """How many eligible cases each metric actually produced a score for.

    An aggregate hides this, and it is the difference between a result and an anecdote: a
    faithfulness of 1.00 over two of twelve cases is not the same claim as 1.00 over
    twelve. Individual metric jobs can fail — a provider timeout, a rejected parameter —
    and a mean over whatever survived would quietly describe a different set each run.
    """
    eligible_count = sum(1 for r in results if r.usable)
    return {metric: (sum(1 for r in results if r.usable and r.scores.get(metric) is not None),
                     eligible_count)
            for metric in METRICS}
