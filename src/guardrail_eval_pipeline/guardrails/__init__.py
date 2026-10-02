"""Guardrail checks, independent of how a request arrived.

`check_input(text)` asks a question about a piece of text and returns a structured
`Verdict`. Nothing here knows about HTTP, FastAPI or any particular target, so the proxy
is one caller and a Python agent importing it directly is another.

Every path returns a Verdict. A guardrail that cannot reach its backend, times out, or
answers something unrecognised returns `blocked=True, failed_closed=True` — a check that
did not happen is not a check that passed.
"""

from guardrail_eval_pipeline.guardrails.bedrock import BedrockGuardrail

__all__ = ["BedrockGuardrail"]
