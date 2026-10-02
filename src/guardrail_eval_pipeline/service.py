"""The guarded path: check the question, ask the target, check the answer.

Two callers share this function rather than an HTTP contract — the API, and the evaluation
runner in process — so the runner cannot evaluate an unguarded system by mistake.

No timing is taken here. Latency and token usage come from the trace, per stage rather
than as one number, and are reported unavailable when tracing is off rather than
fabricated as zero.

The whole request is one span, and the target's own spans nest inside it: `ask` is handed
this span's context, which the target adopts as its parent. Without that the two processes
produce two unrelated traces and nobody can see a retrieval and the guardrail decision
about it side by side.

The input guardrail runs here. The output one arrives in B3; its seam is marked below.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from langsmith import get_current_run_tree, traceable

from guardrail_eval_pipeline.contracts import Target, TargetResponse, Verdict


class Guard(Protocol):
    """Anything that can judge a question and an answer. Structural, so the service depends
    on the checks rather than on Bedrock."""

    def check_input(self, text: str) -> Verdict: ...

    def check_output(self, response: TargetResponse, *, question: str,
                     allowed_scopes: list[str] | None) -> Verdict: ...


@dataclass
class Guarded:
    """One answered question, with what the guardrails decided about it.

    `blocked` is the guardrails' verdict and is what the renderer reads. It is not the
    same as `response.refused`, which is true when the TARGET declined — those refusals
    are the target behaving correctly and keep their own message.
    """

    response: TargetResponse
    blocked: bool = False
    verdict: Verdict | None = None

    @property
    def principal(self) -> str | None:
        """Return who the target says it answered as.

        Only the target can verify its own credentials, so this is authoritative. It is
        None when the target reports no principal, or when it was never called.
        """
        return self.response.principal


def _trace_headers() -> dict[str, str] | None:
    """This span's context, in the headers a target can adopt as its parent.

    None when tracing is off, which is also what the target expects: an empty mapping
    would start a fresh trace there, which is what happens anyway.
    """
    run = get_current_run_tree()
    return run.to_headers() if run is not None else None


def _record(result: Guarded) -> None:
    """Put the verdict on the span, so traces can be filtered by what was decided.

    A trace that does not say why it was blocked sends a reviewer back to the logs, which
    is the thing having a trace was supposed to avoid.
    """
    run = get_current_run_tree()
    if run is None:
        return
    verdict = result.verdict
    run.metadata.update({
        "blocked": result.blocked,
        "principal": result.principal,
        "guardrails": verdict is not None,
        "failed_closed": bool(verdict and verdict.failed_closed),
        "reasons": list(verdict.reasons) if verdict else [],
        "target_refused": result.response.refused,
    })
    if verdict is not None and verdict.failed_closed:
        run.tags.append("failed-closed")
    run.tags.append("blocked" if result.blocked else
                    "target-refused" if result.response.refused else "answered")


@traceable(run_type="chain", name="guarded request")
def handle(
    question: str,
    *,
    target: Target,
    token: str | None = None,
    principal: str | None = None,
    trace_headers: dict[str, str] | None = None,
    guardrails: Guard | None = None,
) -> Guarded:
    """Answer one question through the guardrails.

    Pass `token` for a caller with its own credential, or `principal` for one
    authenticating from configuration. Raises `TargetError` if the target cannot answer.

    With no `guardrails` the question goes straight to the target. That is how the
    pipeline behaved before this step and how a target with no guardrail configured still
    works; `/health` reports which of the two is running, because an unguarded pipeline
    otherwise looks exactly like a guarded one.
    """
    verdict = guardrails.check_input(question) if guardrails is not None else None

    if verdict is not None and verdict.blocked:
        # Traced like any other outcome. A blocked request is the one a reviewer opens
        # first, so it must not be the one that leaves no trace.
        # The target is never called: a blocked question costs nothing downstream, and the
        # answer carries the generic refusal while `verdict.reasons` stays for the log.
        blocked = Guarded(
            response=TargetResponse(answer=verdict.message or "", principal=principal),
            blocked=True,
            verdict=verdict,
        )
        _record(blocked)
        return blocked

    response = target.ask(question, principal=principal, token=token,
                          trace_headers=trace_headers or _trace_headers())

    if guardrails is None:
        unguarded = Guarded(response=response, verdict=verdict)
        _record(unguarded)
        return unguarded

    # The principal comes back with the response, so the scope check runs against the
    # identity the target actually acted on rather than a claimed one.
    out = guardrails.check_output(
        response,
        question=question,
        allowed_scopes=_scopes_for(target, response.principal),
    )
    if out.blocked:
        withheld = Guarded(
            response=TargetResponse(answer=out.message or "", principal=response.principal),
            blocked=True,
            verdict=out,
        )
        _record(withheld)
        return withheld
    if out.masked_text is not None:
        # Masked, not blocked: the answer stands with entities replaced, metadata intact.
        response = replace(response, answer=out.masked_text)
    allowed = Guarded(response=response, verdict=out)
    _record(allowed)
    return allowed


def _scopes_for(target: Target, principal: str | None) -> list[str] | None:
    """What the target says this principal may read, or None if it will not say.

    An adapter without `allowed_scopes` is not a failure: the check reports unavailable,
    which is not the same as reporting that nothing leaked.
    """
    ask = getattr(target, "allowed_scopes", None)
    if ask is None or principal is None:
        return None
    return ask(principal)
