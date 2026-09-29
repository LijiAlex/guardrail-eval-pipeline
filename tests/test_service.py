"""The shared guarded path.

Identity comes from the target: it is the only party that can verify its own credentials,
so the principal it reports with the answer is the authoritative one.
"""

from __future__ import annotations

from guardrail_eval_pipeline import service
from guardrail_eval_pipeline.adapters.stub import StubTarget, sample_answer
from guardrail_eval_pipeline.contracts import TargetResponse


def test_the_callers_token_reaches_the_target():
    stub = StubTarget()
    service.handle("q", target=stub, token="tok-abc")
    assert stub.calls[-1]["token"] == "tok-abc"


def test_without_a_token_the_configured_principal_is_used():
    """The evaluation runner's path: no browser, no login, credentials from config."""
    stub = StubTarget()
    service.handle("q", target=stub, principal="billing_executive")
    assert stub.calls[-1]["principal"] == "billing_executive"


def test_the_principal_is_whatever_the_target_reported():
    """Not what the caller claimed. The output guardrail's scope check reads this."""
    result = service.handle("q", target=StubTarget({"q": sample_answer()}), token="tok")
    assert result.principal == "doctor"


def test_a_target_that_reports_no_principal_leaves_it_unset():
    stub = StubTarget({"q": TargetResponse(answer="hi")})
    assert service.handle("q", target=stub, token="tok").principal is None


def test_trace_headers_reach_the_target():
    """C1 depends on this: the target's spans nest under ours only if the context arrives."""
    stub = StubTarget()
    service.handle("q", target=stub, token="tok", trace_headers={"langsmith-trace": "t-1"})
    assert stub.calls[-1]["trace_headers"] == {"langsmith-trace": "t-1"}


def test_latency_is_measured_around_the_target_call():
    result = service.handle("q", target=StubTarget({"q": sample_answer()}), principal="doctor")
    assert result.latency_ms >= 0


def test_blocked_defaults_to_false_and_is_the_guardrails_to_set():
    """`blocked` is the guardrails' verdict. It is not `response.refused`, which is true
    when the target itself declined."""
    result = service.handle("q", target=StubTarget({"q": sample_answer()}), principal="doctor")
    assert result.blocked is False
