"""The generic contract, and the second implementation that proves it is generic."""

from __future__ import annotations

from guardrail_eval_pipeline.contracts import Retrieved, Target, TargetResponse, implements_target
from guardrail_eval_pipeline.adapters.stub import StubTarget, sample_answer


def test_a_bare_answer_reports_no_contexts():
    """has_contexts is the signal that separates "could not look" from "found nothing".

    The report reads it so an unavailable metric is never printed as a score of zero.
    """
    assert TargetResponse(answer="hello").has_contexts is False
    assert sample_answer().has_contexts is True


def test_contexts_carry_the_passage_not_just_a_filename():
    """Grounding and three of the four RAGAS metrics score against the text itself."""
    response = sample_answer()
    assert isinstance(response.contexts[0], Retrieved)
    assert "1 g Q8H" in response.contexts[0].text
    assert response.contexts[0].score == 6.38


def test_stub_satisfies_the_target_protocol():
    """Two implementations sharing no transport, auth or vocabulary. That is the point."""
    assert isinstance(StubTarget(), Target)
    assert implements_target(StubTarget())


def test_the_protocol_check_alone_is_not_enough():
    """`runtime_checkable` matches attribute NAMES, not signatures, so a class with a bare
    `def ask(self)` passes `isinstance`. `implements_target` checks the parameters."""

    class Bogus:
        name = "bogus"

        def ask(self):  # nothing like the real signature
            ...

    assert isinstance(Bogus(), Target) is True          # the weakness
    assert implements_target(Bogus()) is False          # what we check instead


def test_stub_returns_what_it_was_seeded_with():
    answer = sample_answer()
    stub = StubTarget({"dose of meropenem?": answer})
    assert stub.ask("dose of meropenem?") is answer


def test_stub_refuses_an_unknown_question_rather_than_inventing_one():
    result = StubTarget().ask("something nobody seeded")
    assert result.refused is True
    assert result.refusal_reason == "not_found"
    assert result.contexts == []


def test_stub_records_the_principal_and_trace_headers_it_was_handed():
    """Trace propagation is otherwise invisible from outside the target (C1 asserts on this)."""
    stub = StubTarget()
    stub.ask("q", principal="nurse", token="tok", trace_headers={"langsmith-trace": "abc"})
    assert stub.calls == [
        {
            "question": "q",
            "principal": "nurse",
            "token": "tok",
            "trace_headers": {"langsmith-trace": "abc"},
        }
    ]
