"""The pipeline's HTTP surface.

    GET  /health              is the pipeline up, and which target is wired in
    POST /login               forwarded to the target, unchanged
    GET  /collections/{role}  forwarded to the target
    POST /chat                the guarded path — a bearer token is required

`/chat` answers in the TARGET's shape, via `adapter.render()`, so an existing UI keeps
working when traffic is routed through here. The generic shape is what `service.handle`
returns and what the evaluation runner consumes.

`/login` and `/collections/{role}` are not questions, so no guardrail applies. They are
forwarded through the adapter; the only target knowledge in this file is those two paths.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator

from guardrail_eval_pipeline import service
from guardrail_eval_pipeline.adapters import build_target
from guardrail_eval_pipeline.config import ConfigError, TargetConfig, load_policy, load_target
from guardrail_eval_pipeline.guardrails import BedrockGuardrail
from guardrail_eval_pipeline.contracts import Target, TargetAuthError, TargetError

DEFAULT_TARGET_CONFIG = "targets/medibot.yaml"

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Release the target's connection pool when the application shuts down.

    The target is cached for the life of the process, so nothing else would close it.
    """
    yield
    close = getattr(get_target(), "close", None)
    if close is not None:
        close()


app = FastAPI(
    title="Guardrail & Evaluation Pipeline",
    description="Guardrails, tracing and evaluation wrapped around any RAG or agentic chatbot.",
    version="0.1.0",
    lifespan=lifespan,
)

# The browser is served from a different port, so every call it sends is cross-origin and
# blocked without this. An allowlist rather than "*": nothing here needs to be callable
# from an arbitrary page.
ALLOWED_ORIGINS = os.getenv("GEP_ALLOWED_ORIGINS", "http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in ALLOWED_ORIGINS if origin.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

@lru_cache(maxsize=1)
def get_config() -> TargetConfig:
    """The wired-in target, from `GEP_TARGET_CONFIG` or `targets/medibot.yaml`.

    The default path resolves against the repository root, not the working directory.
    """
    configured = os.getenv("GEP_TARGET_CONFIG")
    if configured:
        path = configured
    else:
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        path = os.path.join(repo_root, DEFAULT_TARGET_CONFIG)
    try:
        return load_target(path)
    except ConfigError as exc:
        raise RuntimeError(f"cannot start without a usable target config: {exc}") from exc


@lru_cache(maxsize=1)
def get_target() -> Target:
    """Build the adapter named by the target config, once per process."""
    return build_target(get_config())


@lru_cache(maxsize=1)
def get_guardrail() -> BedrockGuardrail | None:
    """The guardrail named by the target config, or None when it names none.

    Resolved once at startup rather than per request, so a misconfigured guardrail is a
    loud failure on the first call instead of a slow one on every call.

    Deliberately not silent: a target with no `guardrails:` block runs unguarded, which is
    a legitimate configuration, and `/health` says which it is.
    """
    policy_path = (get_config().raw.get("guardrails") or {}).get("policy")
    if not policy_path:
        return None
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    if not os.path.isabs(policy_path):
        policy_path = os.path.join(repo_root, policy_path)
    return BedrockGuardrail.from_policy(load_policy(policy_path))


def bearer(authorization: Annotated[str | None, Header()] = None) -> str:
    """The caller's bearer token. 401 if absent or malformed.

    Never verified here — only the target can do that. This layer insists one is present
    so there is no anonymous path answered as a default account.
    """
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="missing bearer token")
    return token.strip()


class ChatRequest(BaseModel):
    """The body of a `/chat` request: a single question, and nothing else."""

    # Unknown keys are a 422, not silently dropped: a caller sending `principal` should
    # learn it is not accepted rather than receive someone else's answer.
    model_config = ConfigDict(extra="forbid")

    # An upper bound as well as a lower one: an unbounded body reaches a paid model.
    question: str = Field(min_length=1, max_length=1000)

    @field_validator("question")
    @classmethod
    def not_only_whitespace(cls, value: str) -> str:
        """Reject a question that contains nothing but whitespace."""
        # min_length counts characters, so "   " passes it.
        if not value.strip():
            raise ValueError("question cannot be blank")
        return value


@app.get("/health")
def health(guardrail: Any = Depends(get_guardrail)) -> dict[str, str | None]:
    """Report that the pipeline is running, which target it watches, and whether the
    guardrails are actually on.

    `guardrail: null` is the answer worth having. An unguarded pipeline serves identical
    responses to a guarded one right up until something should have been blocked.
    """
    config = get_config()
    return {
        "status": "ok",
        "target": config.name,
        "endpoint": config.endpoint,
        "guardrail": getattr(guardrail, "identifier", None),
    }


def _forward(target: Target, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """Call the target's `forward`, translating its failures into HTTP responses.

    Answers 501 when the wired-in target does not support passthrough at all, and 502
    when it does but could not be reached.
    """
    forward = getattr(target, "forward", None)
    if forward is None:
        raise HTTPException(status_code=501, detail=f"{target.name} does not support {path}")
    try:
        return forward(method, path, body=body)
    except TargetError as exc:
        raise HTTPException(status_code=502, detail=f"target unavailable: {exc}") from exc


class LoginRequest(BaseModel):
    """The body of a `/login` request, forwarded to the target unchanged."""

    username: str
    password: str


@app.post("/login")
def login(request: LoginRequest, target: Target = Depends(get_target)) -> dict[str, Any]:
    """Forward a login to the target and return its response unchanged.

    The pipeline neither validates credentials nor stores anything from them. This exists
    so that a browser routed here has one address for the whole API rather than two.
    """
    status, body = _forward(target, "POST", "/login", request.model_dump())
    if status != 200:
        raise HTTPException(status_code=status, detail=body.get("detail", "login failed"))
    return body


@app.get("/collections/{role}")
def collections(role: str, target: Target = Depends(get_target)) -> dict[str, Any]:
    """Forward a capability lookup to the target, for a role this config knows about."""
    # Checked before being interpolated into a URL. A path parameter never matches "/",
    # but "?" and "#" pass through.
    if role not in get_config().principals:
        raise HTTPException(status_code=404, detail=f"unknown role: {role}")
    status, body = _forward(target, "GET", f"/collections/{role}")
    if status != 200:
        raise HTTPException(status_code=status, detail=body.get("detail", "not available"))
    return body


@app.post("/chat")
def chat(
    request: ChatRequest,
    target: Target = Depends(get_target),
    token: str = Depends(bearer),
    guardrail: Any = Depends(get_guardrail),
) -> dict[str, Any]:
    """One question, guarded, answered in the target's own shape.

    A bearer token is required; there is no anonymous path. The evaluation runner does not
    come through here — it calls `service.handle` directly and authenticates from
    configuration, so both share the guarded logic without sharing this endpoint's
    identity rules.
    """
    try:
        result = service.handle(
            request.question,
            target=target,
            token=token,
            guardrails=guardrail,
        )
    except TargetAuthError as exc:
        # The caller needs to sign in again, not wait for a system to recover.
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except TargetError as exc:
        raise HTTPException(status_code=502, detail=f"target unavailable: {exc}") from exc

    render = getattr(target, "render", None)
    if render is None:
        # A target with no UI of its own has no shape to render back into.
        return {
            "answer": result.response.answer,
            "citations": [] if result.blocked else result.response.citations,
            "refused": result.response.refused or result.blocked,
            "refusal_reason": "blocked" if result.blocked else result.response.refusal_reason,
            "principal": result.principal,
        }
    # The guardrails' verdict, not the target's: a target that refused on its own terms
    # keeps its own message.
    return render(result.response, withhold=result.blocked)
