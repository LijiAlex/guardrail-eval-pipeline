"""The guarded path: check the question, ask the target, check the answer.

Two callers share this function rather than an HTTP contract — the API, and the evaluation
runner in process — so the runner cannot evaluate an unguarded system by mistake.

No timing is taken here. Latency and token usage both come from the trace, where they are
recorded per stage rather than as one number for the whole request, and where nothing has
to be maintained by hand. With tracing switched off they are reported **unavailable** — the
same rule this pipeline applies to every other measurement it could not take, because a
zero that means "we did not look" is indistinguishable from one that means "it failed".

The input guardrail runs here. The output one arrives in B3; its seam is marked below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from guardrail_eval_pipeline.contracts import Target, TargetResponse, Verdict


class InputGuard(Protocol):
    """Anything that can judge a question. Kept structural so the service depends on the
    check rather than on Bedrock."""

    def check_input(self, text: str) -> Verdict: ...


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


def handle(
    question: str,
    *,
    target: Target,
    token: str | None = None,
    principal: str | None = None,
    trace_headers: dict[str, str] | None = None,
    guardrails: InputGuard | None = None,
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
        # The target is never called: a blocked question costs nothing downstream, and the
        # answer carries the generic refusal while `verdict.reasons` stays for the log.
        return Guarded(
            response=TargetResponse(answer=verdict.message or "", principal=principal),
            blocked=True,
            verdict=verdict,
        )

    response = target.ask(question, principal=principal, token=token, trace_headers=trace_headers)

    # SEAM (B3): check_output(response.answer, response.contexts, response.principal).
    # The principal arrives with the response, from the only party that can verify it.

    return Guarded(response=response, verdict=verdict)
