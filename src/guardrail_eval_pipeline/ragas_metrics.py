"""The four RAGAS metrics the spec names, over the cases that can carry them.

    faithfulness        is the answer supported by the passages
    answer relevancy    does it address the question
    context precision   were the retrieved passages the useful ones
    context recall      did retrieval find what the reference answer needed

Three of those need passages, so a case the target answered from records has nothing for
them to measure. Those are reported UNAVAILABLE. A zero would be indistinguishable from a
system that retrieved badly, and only one of those is a fault.

The evaluator must not be the model that wrote the answers. Faithfulness decomposes an
answer into claims and checks each against the passages, and a model asked to grade its own
phrasing is charitable to it — the same circularity the judge avoids.

It ran on `openai/gpt-oss-20b` until that model's daily quota was exhausted, and now runs on
`qwen/qwen3.8-27b`. That shares a model with the judge, which is a weaker separation than
before but the right one to give up: the judge and these metrics measure different things,
while evaluating the target with the target's own model would be circular. The remaining
candidate, `gpt-oss-120b`, *is* the target's model.

Embeddings come from Bedrock and spend no provider quota at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Only cases that produced an answer AND the passages behind it can be scored.
METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")

# The RAGAS evaluator runs on OpenAI when OPENAI_API_KEY is set, and falls back to Groq
# otherwise. Groq's free tier caps this organisation at 200,000 tokens per DAY and a single
# full evaluation spends most of it, so runs were ending with scores missing — and missing
# in a biased way, because the set is scored in order and the long cases are at the end.
#
# The JUDGE deliberately does not follow it there. The target answers on
# `openai/gpt-oss-120b`, so grading with another OpenAI model is weaker separation than a
# different family from a different company. See judge.py.
EVALUATOR_MODEL = "qwen/qwen3.8-27b"
OPENAI_EVALUATOR_MODEL = "gpt-4.1-mini"
EMBEDDING_MODEL = "cohere.embed-english-v3"
# Bounded so that `strictness` calls for one case still fit inside the provider's
# per-minute output cap, which is what a declared-but-unbounded request blows through.
EVALUATOR_MAX_TOKENS = 400
# RAGAS' default, and what the metric's conventional reading assumes. It ran at 1 while the
# evaluator was rate-limited hard enough that three generations per case cost an extra half
# hour; on OpenAI that constraint is gone, so the metric runs as designed.
#
# Measured at both on Groq, six cases spanning the observed range: the mean moves
# 0.681 -> 0.647, five of six by less than 0.013. The default scores this system slightly
# LOWER, so this is not the flattering setting — docs/measurements/strictness.json has
# every figure.
ANSWER_RELEVANCY_STRICTNESS = 3
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
    from langchain_core.rate_limiters import InMemoryRateLimiter
    from langchain_groq import ChatGroq
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper

    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("GROQ_API_KEY")):
        raise RuntimeError("neither OPENAI_API_KEY nor GROQ_API_KEY is set; RAGAS cannot run")
    # Temperature zero: the spec asks that running twice against an unchanged system give
    # consistent results, and these metrics are themselves model calls.
    #
    # A generous timeout and retries because the provider rate-limits, and a metric that
    # silently drops cases is worse than one that takes longer.
    #
    # `max_tokens` is not optional here. The provider rejects a request whose *expected*
    # output exceeds its per-minute cap, so a call that declares no bound is refused
    # outright — "Limit 1000, Requested 1737" — and the score is lost. The judge bounds
    # its calls the same way and loses none.
    #
    # The rate limiter spends that per-minute budget deliberately rather than letting
    # RAGAS' executor exhaust it in the first few seconds. One request per 45s against a
    # 700-token bound stays inside a 1000 output-tokens-per-minute cap. Scoring is slow
    # as a result, which is the right trade: an incomplete metric is worth less than a
    # complete one that took half an hour.
    if os.environ.get("OPENAI_API_KEY"):
        from langchain_openai import ChatOpenAI

        # No daily cap and rate limits high enough that the pacing above is unnecessary,
        # so the run finishes in minutes rather than the better part of an hour. `n>1` is
        # supported, so `strictness` needs no `bypass_n` workaround.
        llm = ChatOpenAI(model=OPENAI_EVALUATOR_MODEL, temperature=0, timeout=120,
                         max_retries=5)
        wrapped = LangchainLLMWrapper(llm)
    else:
        llm = ChatGroq(
            model=EVALUATOR_MODEL,
            temperature=0,
            timeout=120,
            max_retries=5,
            max_tokens=EVALUATOR_MAX_TOKENS,
            rate_limiter=InMemoryRateLimiter(
                requests_per_second=1 / 30,
                check_every_n_seconds=1,
                max_bucket_size=1,
            ),
        )
        # `bypass_n`: send the prompt n times rather than asking for n completions,
        # which this provider refuses with `400 'n' : number must be at most 1`.
        wrapped = LangchainLLMWrapper(llm, bypass_n=True)

    # Embeddings stay on Bedrock whichever LLM is used. The scale is measured and healthy
    # (0.977 for a close paraphrase, 0.126 for an unrelated question), and changing the
    # embedder would move every answer_relevancy score and invalidate the comparisons in
    # docs/measurements/.
    embeddings = BedrockEmbeddings(model_id=EMBEDDING_MODEL, region_name=EMBEDDING_REGION)
    return wrapped, LangchainEmbeddingsWrapper(embeddings)


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

        # answer_relevancy reverse-generates `strictness` questions from the answer and
        # averages their similarity to the question actually asked. RAGAS asks for them as
        # `n` completions, which this provider rejects outright — "'n' : number must be at
        # most 1" — so the wrapper below is built with `bypass_n`, which sends the prompt
        # that many times instead. Three separate calls, no `n` parameter.
        #
        # Three is RAGAS' default; see ANSWER_RELEVANCY_STRICTNESS for why this runs at
        # one and what the difference measures out to.
        answer_relevancy.strictness = ANSWER_RELEVANCY_STRICTNESS

        frame = evaluate(
            dataset,
            metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
            llm=llm, embeddings=embeddings,
            # One worker, because the rate limiter above already serialises the calls and
            # a second worker would only queue behind it.
            #
            # The timeout has to cover the wait for a rate-limiter token as well as the
            # call itself: the limiter blocks inside the request, so a bound shorter than
            # the pacing interval fails every job that waits for one. At one request per
            # 45s, 90s was shorter than the wait and killed jobs that were doing nothing
            # wrong.
            #
            # A throttled or timed-out job comes back as a missing score rather than an
            # error, so coverage degrades quietly under load — the `coverage()` figure in
            # the report is what makes that visible instead of silently shrinking the
            # denominator.
            run_config=RunConfig(max_workers=1, timeout=300, max_retries=3),
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
