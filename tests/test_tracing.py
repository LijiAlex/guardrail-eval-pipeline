"""One trace across both processes.

Asserted against LangSmith's in-memory run tree rather than the hosted service, so these
run offline and cost nothing. `tracing_context(enabled="local")` builds the tree without
sending it anywhere; what the service adds is storage and a viewer, not a different tree.
"""

from __future__ import annotations

from langsmith.run_helpers import get_current_run_tree, trace, tracing_context

from guardrail_eval_pipeline import service
from guardrail_eval_pipeline.adapters.stub import StubTarget, sample_answer
from tests.test_guardrails import ALLOWED, BLOCKED, guard


def stub() -> StubTarget:
    return StubTarget({"dose of meropenem?": sample_answer()})


def run(question="dose of meropenem?", **kwargs):
    """Answer one question inside a local trace, and hand back the root span."""
    with tracing_context(enabled="local"):
        with trace("test", run_type="chain") as root:
            result = service.handle(question, **kwargs)
    return root, result


def span_named(root, name):
    for child in root.child_runs or []:
        if child.name == name:
            return child
        found = span_named(child, name)
        if found is not None:
            return found
    return None


def test_a_request_is_one_span():
    root, _ = run(target=stub())
    assert span_named(root, "guarded request") is not None


def test_the_target_is_handed_this_spans_context():
    """The whole point: without these headers the target starts its own trace and the two
    processes produce two unrelated halves of one request."""
    target = stub()
    run(target=target)
    sent = target.calls[-1]["trace_headers"]
    assert "langsmith-trace" in sent


def test_the_headers_identify_the_span_the_target_should_nest_under():
    """What the target does with them is its own business; this asserts they name a real
    parent rather than being present and empty."""
    target = stub()
    root, _ = run(target=target)
    guarded = span_named(root, "guarded request")
    sent = target.calls[-1]["trace_headers"]["langsmith-trace"]
    assert str(guarded.trace_id) in sent


def test_each_guardrail_check_is_its_own_span():
    """So a reviewer can see what the input check decided separately from the output one,
    and what each cost."""
    root, _ = run(target=stub(), guardrails=guard(ALLOWED))
    assert span_named(root, "guardrail input") is not None
    assert span_named(root, "guardrail output") is not None


def test_a_blocked_request_still_produces_a_trace():
    """The one a reviewer opens first. A block returns before the target is called, which
    is exactly the path most likely to leave nothing behind."""
    target = stub()
    root, result = run(target=target, guardrails=guard(BLOCKED))
    assert result.blocked is True
    assert target.calls == []
    guarded = span_named(root, "guarded request")
    assert guarded is not None
    assert "blocked" in guarded.tags


def test_the_span_records_why():
    """A trace that does not say why it blocked sends the reviewer back to the logs."""
    root, _ = run(target=stub(), guardrails=guard(BLOCKED))
    guarded = span_named(root, "guarded request")
    assert guarded.metadata["blocked"] is True
    assert set(guarded.metadata["reasons"]) == {"UnauthorisedRoleClaim", "PROMPT_ATTACK"}
    assert guarded.metadata["failed_closed"] is False


def test_an_answered_request_is_tagged_as_answered():
    root, _ = run(target=stub(), guardrails=guard(ALLOWED))
    guarded = span_named(root, "guarded request")
    assert "answered" in guarded.tags
    assert guarded.metadata["blocked"] is False


def test_a_target_refusal_is_tagged_apart_from_a_block():
    """Three outcomes, not two: we blocked, the target refused, or it answered. A report
    that merged the first two would read the target behaving correctly as an attack."""
    root, result = run("nobody seeded this", target=StubTarget({}), guardrails=guard(ALLOWED))
    guarded = span_named(root, "guarded request")
    assert result.blocked is False
    assert "target-refused" in guarded.tags


def test_failing_closed_is_tagged_separately():
    """An outage is not a wave of attacks, and the tag is what keeps them apart in a
    dashboard built on these traces."""
    root, _ = run(target=stub(), guardrails=guard(TimeoutError("read timeout")))
    guarded = span_named(root, "guarded request")
    assert "failed-closed" in guarded.tags
    assert guarded.metadata["failed_closed"] is True


def test_nothing_is_traced_when_tracing_is_off():
    """Off is the default, and a decorated function must then cost nothing and record
    nothing — including the headers, which would otherwise name a span that does not exist."""
    target = stub()
    with tracing_context(enabled=False):
        assert get_current_run_tree() is None
        service.handle("dose of meropenem?", target=target)
    assert target.calls[-1]["trace_headers"] == {}
