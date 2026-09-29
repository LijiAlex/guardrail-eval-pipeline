"""A target that answers from a dictionary. No network, no model, no cost.

Lets every other component be tested without a live target — which matters because
MediBot holds an embedded Qdrant lock (one process at a time) and spends Groq tokens
against a daily cap. Being a second implementation of `Target` that shares none of
MediBot's transport, auth or vocabulary, it also shows the contract is not MediBot's
shape under another name.
"""

from __future__ import annotations

from guardrail_eval_pipeline.contracts import Retrieved, TargetResponse


class StubTarget:
    """Answers from a canned mapping; unknown questions get a not-found refusal.

    Matching is on the exact question string, so a failing test means a broken pipeline
    rather than a near-miss lookup. `calls` records what each `ask` was handed.
    """

    name = "stub"

    def __init__(self, answers: dict[str, TargetResponse] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[dict[str, object]] = []

    def ask(
        self,
        question: str,
        *,
        principal: str | None = None,
        token: str | None = None,
        trace_headers: dict[str, str] | None = None,
    ) -> TargetResponse:
        """Return the canned answer for this question, or a not-found refusal.

        Everything the call was handed is appended to `calls`, so a test can assert what
        the pipeline forwarded.
        """
        self.calls.append(
            {
                "question": question,
                "principal": principal,
                "token": token,
                "trace_headers": trace_headers or {},
            }
        )
        if question in self.answers:
            return self.answers[question]
        return TargetResponse(
            answer="I don't have anything on that.",
            refused=True,
            refusal_reason="not_found",
            raw={"stub": True, "question": question},
        )


def sample_answer(
    answer: str = "The standard dose of meropenem is 1 g every 8 hours. [1]",
    context: str = "Meropenem. Standard Dose = 1 g Q8H. Tier = 3.",
) -> TargetResponse:
    """Build a grounded, citing answer of the kind a healthy target returns.

    Useful as the baseline in a test, so that a check firing means the check found
    something rather than that the fixture was malformed.
    """
    return TargetResponse(
        answer=answer,
        contexts=[
            Retrieved(
                text=context,
                score=6.38,
                label="drug_formulary.pdf / Antimicrobials",
                scope="clinical",
            )
        ],
        citations=["drug_formulary.pdf"],
        principal="doctor",
        tokens={"input_tokens": 420, "output_tokens": 31, "total_tokens": 451},
        timings={"total_ms": 900.0},
        raw={"stub": True},
    )
