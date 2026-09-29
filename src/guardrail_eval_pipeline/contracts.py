"""The contract between this pipeline and the system it watches.

Guardrails, heuristics, RAGAS, the judge and the report all read `TargetResponse`. None of
them knows which system answered. Supporting a new system means writing one adapter.

The vocabulary is deliberately not any one target's:

    principal   who is asking. Optional — not every system has access control.
    contexts    the passages the system retrieved, as text. Grounding checks and three of
                the four RAGAS metrics score the passage itself, so a filename will not do.
    citations   what the answer points at.
    scope       the access zone a passage belongs to.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class TargetError(RuntimeError):
    """Raised when the target cannot be reached, or answers with something unusable.

    Every adapter raises this one type, so a caller can handle failure without knowing
    which system is wired in.
    """


class TargetAuthError(TargetError):
    """Raised when the target rejects the caller's own credential.

    This is kept separate from `TargetError` so that the API can answer 401 rather than
    502. The two mean different things to the person at the other end: one asks them to
    sign in again, the other asks them to wait for a system to come back.
    """


@dataclass
class Retrieved:
    """One passage the target retrieved on its way to an answer.

    Only `text` is required. `score` and `label` are whatever the target exposes — a
    reranker score, a heading breadcrumb — and are reported, never used for a pass/fail
    decision. `scope` names the access zone the passage came from, which the output
    guardrail compares against what the principal may read; `None` means the target has no
    notion of zones and the check reports unavailable.
    """

    text: str
    score: float | None = None
    label: str | None = None
    scope: str | None = None


@dataclass
class TargetResponse:
    """One answer from the target, normalised.

    `principal` is who the TARGET says it answered as, which is authoritative — only the
    target can verify its own credentials. `raw` keeps the untouched response body, for
    rendering back into the target's shape and for a reviewer reading a trace.
    """

    answer: str
    contexts: list[Retrieved] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    refused: bool = False
    refusal_reason: str | None = None
    principal: str | None = None
    tokens: dict[str, int] | None = None
    timings: dict[str, float] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_contexts(self) -> bool:
        """Return whether grounding and the context-based RAGAS metrics can run at all.

        The report reads this to print "unavailable" rather than a score of zero. A zero
        meaning "we could not look" is indistinguishable from one meaning "it failed".
        """
        return bool(self.contexts)


@runtime_checkable
class Target(Protocol):
    """A system this pipeline can put questions to.

    Implementations own transport, authentication and the shape they translate from. They
    own nothing about what a good answer is.
    """

    name: str

    def ask(
        self,
        question: str,
        *,
        principal: str | None = None,
        token: str | None = None,
        trace_headers: dict[str, str] | None = None,
    ) -> TargetResponse:
        """Put one question to the target and return its answer, normalised.

        `token` is the caller's own credential, forwarded untouched. When present it wins:
        the adapter must not mint its own and must not trust `principal`, since a body
        field is typed by a client and a signed token is not. `principal` is for callers
        with no credential of their own, authenticating from configuration.

        `trace_headers` carries tracing context so the target's spans nest under this
        pipeline's trace. A target that ignores it still works.
        """
        ...


REQUIRED_ASK_PARAMS = ("question", "principal", "token", "trace_headers")


def implements_target(candidate: object) -> bool:
    """Return whether `candidate` satisfies `Target`, signature included.

    `isinstance(x, Target)` only checks that an `ask` attribute exists, so a class with a
    bare `def ask(self)` passes it. This checks the parameters too.
    """
    ask = getattr(candidate, "ask", None)
    if not callable(ask) or not getattr(candidate, "name", None):
        return False
    try:
        params = inspect.signature(ask).parameters
    except (TypeError, ValueError):
        return False
    return all(name in params for name in REQUIRED_ASK_PARAMS)


# Three methods a target MAY also provide. They are not on the Protocol because a target
# reachable only as an API has no use for them, and a missing one is a capability this
# pipeline reports as unavailable rather than an error.
#
#   render(result, *, withhold) -> dict
#       This pipeline's shape rendered back into the target's own, so an existing UI keeps
#       working when traffic is routed through here.
#
#   forward(method, path, *, body, headers) -> tuple[int, dict]
#       A non-chat request passed through untouched — a login, a capability lookup.
#
#   allowed_scopes(principal) -> list[str] | None
#       The access zones a principal may read, for the output guardrail's leak check.
#       None means the target has no zones.
