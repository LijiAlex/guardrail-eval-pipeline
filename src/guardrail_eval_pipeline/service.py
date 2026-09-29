"""The guarded path: check the question, ask the target, check the answer.

Two callers share this function rather than an HTTP contract — the API, and the evaluation
runner in process — so the runner cannot evaluate an unguarded system by mistake.

The guardrails themselves arrive in B2 and B3; the two seams are marked below.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from guardrail_eval_pipeline.contracts import Target, TargetResponse


@dataclass
class Guarded:
    """One answered question, with what the guardrails decided about it.

    `blocked` is the guardrails' verdict and is what the renderer reads. It is not the
    same as `response.refused`, which is true when the TARGET declined — those refusals
    are the target behaving correctly and keep their own message.
    """

    response: TargetResponse
    latency_ms: float
    blocked: bool = False

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
) -> Guarded:
    """Answer one question through the guardrails.

    Pass `token` for a caller with its own credential, or `principal` for one
    authenticating from configuration. Raises `TargetError` if the target cannot answer.
    """
    # SEAM (B2): check_input(question). A block ends the request here with a generic
    # refusal, the reason going to the log only. The checks are properties of the text,
    # so they do not need to know who is asking.

    started = time.perf_counter()
    response = target.ask(question, principal=principal, token=token, trace_headers=trace_headers)
    latency_ms = (time.perf_counter() - started) * 1000

    # SEAM (B3): check_output(response.answer, response.contexts, response.principal).
    # The principal arrives with the response, from the only party that can verify it.

    return Guarded(response=response, latency_ms=round(latency_ms, 1))
