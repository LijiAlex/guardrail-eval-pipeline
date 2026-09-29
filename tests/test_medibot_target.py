"""The MediBot adapter: identity, failure handling, and translation in both directions.

Driven through `httpx.MockTransport`, so the whole mapping runs in-process — free,
offline, and unaffected by whether a MediBot server happens to be up. The mapping is the
part that can be wrong; the network is not.

Two response bodies are used deliberately: what MediBot returns today, and what it will
return once A1 adds the `eval` envelope. Both are asserted now, so A1 lands against a
test that already describes it rather than one written afterwards to match it.
"""

from __future__ import annotations

import httpx
import pytest

from guardrail_eval_pipeline.adapters.medibot import MediBotTarget
from guardrail_eval_pipeline.config import TargetConfig
from guardrail_eval_pipeline.contracts import TargetAuthError, TargetError, TargetResponse

BODY_TODAY = {
    "answer": "The standard dose of meropenem is 1 g every 8 hours (Q8H) [1].",
    "sources": [
        {"source_document": "drug_formulary.pdf", "section_title": "1. Antimicrobials", "collection": "clinical"},
        {"source_document": "drug_formulary.pdf", "section_title": "Renal Dose Adjustment", "collection": "clinical"},
    ],
    "retrieval_type": "hybrid_rag",
    "role": "doctor",
    "sql": None,
    "refusal": None,
}

BODY_AFTER_A1 = {
    **BODY_TODAY,
    "eval": {
        "contexts": [
            {
                "text": "Meropenem. Standard Dose = 1 g Q8H. Tier = 3.",
                "score": 6.38,
                "source_document": "drug_formulary.pdf",
                "section_title": "1. Antimicrobials",
                "collection": "clinical",
            }
        ]
    },
}

BODY_REFUSAL = {
    "answer": "As a nurse, you don't have access to billing documents.",
    "sources": [],
    "retrieval_type": "hybrid_rag",
    "role": "nurse",
    "sql": None,
    "refusal": "role",
}


def config() -> TargetConfig:
    return TargetConfig(
        name="medibot",
        adapter="medibot",
        endpoint="http://testserver",
        principals={"doctor": "dr.mehta", "nurse": "nurse.priya"},
        auth={"kind": "jwt_login", "password_suffix": "-demo"},
    )


def make_target(chat_body: dict, *, login_status: int = 200, chat_status: int = 200,
                record: list | None = None) -> MediBotTarget:
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        if request.url.path == "/login":
            if login_status != 200:
                return httpx.Response(login_status, json={"detail": "incorrect username or password"})
            return httpx.Response(200, json={"token": "minted.by.us", "role": "doctor"})
        if request.url.path == "/chat":
            return httpx.Response(chat_status, json=chat_body)
        if request.url.path.startswith("/collections/"):
            return httpx.Response(200, json={"role": "nurse", "collections": ["general", "nursing"]})
        return httpx.Response(404, json={"detail": "nope"})

    return MediBotTarget(config(), client=httpx.Client(transport=httpx.MockTransport(handler)))


# --- identity ---------------------------------------------------------------
def test_a_callers_token_is_forwarded_and_no_login_happens():
    """The browser's own credential. The target is the only party that can verify it."""
    seen: list[httpx.Request] = []
    make_target(BODY_TODAY, record=seen).ask("q", token="caller.jwt.value")
    assert [r.url.path for r in seen] == ["/chat"]
    assert seen[0].headers["authorization"] == "Bearer caller.jwt.value"


def test_a_token_beats_a_claimed_principal():
    """The forgery path. A body field is typed by a client; a signed token is not."""
    seen: list[httpx.Request] = []
    make_target(BODY_TODAY, record=seen).ask("q", principal="admin-i-am-not", token="caller.jwt.value")
    assert [r.url.path for r in seen] == ["/chat"]
    assert seen[0].headers["authorization"] == "Bearer caller.jwt.value"


def test_the_principal_comes_back_from_the_target_not_from_the_caller():
    assert make_target(BODY_REFUSAL).ask("q", token="whoever").principal == "nurse"


def test_without_a_token_it_authenticates_from_configuration():
    """The evaluation runner's path."""
    seen: list[httpx.Request] = []
    make_target(BODY_TODAY, record=seen).ask("q", principal="doctor")
    assert [r.url.path for r in seen] == ["/login", "/chat"]


@pytest.mark.parametrize("principal", ["", None])
def test_a_blank_principal_raises_rather_than_becoming_the_first_account(principal):
    """A blank principal must raise, not fall through to the first configured account —
    which would answer the question with whatever access that account happens to have."""
    target = MediBotTarget(
        TargetConfig(name="m", adapter="medibot", endpoint="http://t", principals={}),
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
    )
    with pytest.raises(TargetError, match="principal is required"):
        target.ask("q", principal=principal)


def test_an_unconfigured_principal_does_not_leak_the_roster():
    """This message reaches an API caller; listing every account would be a free directory."""
    with pytest.raises(TargetError) as exc:
        make_target(BODY_TODAY).ask("q", principal="pharmacist")
    message = str(exc.value)
    assert "pharmacist" in message
    assert "dr.mehta" not in message and "nurse.priya" not in message


def test_a_failed_login_reports_the_status_and_not_the_credential():
    with pytest.raises(TargetError) as exc:
        make_target(BODY_TODAY, login_status=401).ask("q", principal="doctor")
    message = str(exc.value)
    assert "401" in message
    assert "-demo" not in message and "dr.mehta" not in message


def test_the_token_is_fetched_once_and_reused():
    seen: list[httpx.Request] = []
    target = make_target(BODY_TODAY, record=seen)
    target.ask("one", principal="doctor")
    target.ask("two", principal="doctor")
    assert [r.url.path for r in seen] == ["/login", "/chat", "/chat"]


def test_an_expired_token_triggers_exactly_one_re_login():
    """A cached token invalidated by a target restart is retried once, so a run that
    outlives a restart does not fail with 401s that look like a target bug."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/login":
            return httpx.Response(200, json={"token": "fresh", "role": "doctor"})
        if calls.count("/login") >= 1:
            return httpx.Response(200, json=BODY_TODAY)
        return httpx.Response(401, json={"detail": "expired"})

    target = MediBotTarget(config(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    target._tokens["doctor"] = "stale"
    result = target.ask("q", principal="doctor")
    assert result.answer.startswith("The standard dose")
    assert calls == ["/chat", "/login", "/chat"]


# --- failure handling -------------------------------------------------------
def test_a_second_consecutive_401_gives_up_instead_of_looping():
    """The bound the retry test names but does not check. A target that 401s whatever we
    send must end the request, not re-login forever."""
    calls: list[str] = []

    def always_401(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/login":
            return httpx.Response(200, json={"token": "fresh", "role": "doctor"})
        return httpx.Response(401, json={"detail": "expired"})

    target = MediBotTarget(config(), client=httpx.Client(transport=httpx.MockTransport(always_401)))
    target._tokens["doctor"] = "stale"
    with pytest.raises(TargetAuthError):
        target.ask("q", principal="doctor")
    assert calls == ["/chat", "/login", "/chat"]      # exactly one retry, then stop


def test_an_unreachable_target_is_a_TargetError_not_a_raw_transport_error():
    """The target not running is the most common failure; it must arrive as a
    `TargetError` so the API can answer 502 rather than 500."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    target = MediBotTarget(config(), client=httpx.Client(transport=httpx.MockTransport(refuse)))
    with pytest.raises(TargetError, match="cannot reach the target"):
        target.ask("q", token="t")


def test_a_non_json_body_is_a_TargetError():
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    target = MediBotTarget(config(), client=httpx.Client(transport=httpx.MockTransport(html)))
    with pytest.raises(TargetError, match="non-JSON"):
        target.ask("q", token="t")


def test_a_non_200_chat_is_a_TargetError():
    with pytest.raises(TargetError, match="HTTP 503"):
        make_target(BODY_TODAY, chat_status=503).ask("q", token="t")


def test_a_rejected_caller_token_is_an_auth_error_not_a_transport_error():
    """So the API can answer 401 and the UI can say "sign in again", instead of a 502
    that reads as "the system is down"."""
    with pytest.raises(TargetAuthError):
        make_target(BODY_TODAY, chat_status=401).ask("q", token="expired.caller.token")


# --- translation: target -> pipeline ----------------------------------------
def test_today_medibot_gives_no_contexts_and_that_is_reported_honestly():
    result = make_target(BODY_TODAY).ask("dose?", token="t")
    assert result.answer.startswith("The standard dose of meropenem")
    assert result.contexts == []
    assert result.has_contexts is False
    assert result.tokens is None


def test_the_envelope_becomes_contexts_with_scores_and_scope():
    """The envelope carries the passages only. Token usage and stage durations come from
    the trace, not from the target's response body."""
    result = make_target(BODY_AFTER_A1).ask("dose?", token="t")
    assert result.has_contexts is True
    context = result.contexts[0]
    assert context.text == "Meropenem. Standard Dose = 1 g Q8H. Tier = 3."
    assert context.score == 6.38
    assert context.label == "drug_formulary.pdf / 1. Antimicrobials"
    # The field the output scope check reads.
    assert context.scope == "clinical"
    assert result.tokens is None
    assert result.timings is None


def test_citations_come_from_sources_even_when_contexts_do_not():
    assert make_target(BODY_TODAY).ask("dose?", token="t").citations == [
        "drug_formulary.pdf",
        "drug_formulary.pdf",
    ]


def test_a_refusal_keeps_medibots_own_reason_code():
    result = make_target(BODY_REFUSAL).ask("billing codes?", token="t")
    assert result.refused is True
    assert result.refusal_reason == "role"
    assert result.citations == []


def test_trace_headers_are_forwarded_to_the_target():
    seen: list[httpx.Request] = []
    make_target(BODY_TODAY, record=seen).ask("q", token="t", trace_headers={"langsmith-trace": "t-123"})
    assert seen[0].headers["langsmith-trace"] == "t-123"


# --- translation: pipeline -> target ----------------------------------------
SHAPE = {"answer", "sources", "retrieval_type", "role", "sql", "refusal"}

# What a SQL answer looks like when MediBot succeeded: real sources, real SQL. This is
# the only state where withholding matters — MediBot's own refusals already carry
# sources=[] and sql=None, verified in its `RagResult` defaults.
BODY_SQL = {
    "answer": "Neurology submitted 14 claims.",
    "sources": [{"source_document": "mediassist.db", "section_title": "claims", "collection": "sql"}],
    "retrieval_type": "sql_rag", "role": "billing_executive",
    "sql": "SELECT patient_name, approved_amount FROM claims WHERE department='Neurology'",
    "refusal": None,
}


def test_state_a_an_allowed_answer_passes_the_targets_metadata_through():
    result = make_target(BODY_AFTER_A1).ask("dose?", token="t")
    rendered = MediBotTarget.render(result)
    assert set(rendered) == SHAPE
    assert "eval" not in rendered                       # our envelope, never the UI's
    assert rendered["sources"] == BODY_AFTER_A1["sources"]
    assert rendered["role"] == "doctor"


def test_state_b_a_targets_own_refusal_keeps_its_specific_message():
    """The target's own refusal names the collection on purpose. `withhold` is the
    guardrails' verdict and is False here, so that message survives."""
    result = make_target(BODY_REFUSAL).ask("billing?", token="t")
    rendered = MediBotTarget.render(result)
    assert rendered["answer"] == BODY_REFUSAL["answer"]
    assert "billing documents" in rendered["answer"]
    assert rendered["refusal"] == "role"


def test_state_c_a_withheld_answer_ships_no_evidence_of_what_was_blocked():
    """The target answered and the output guardrail blocked it. The SQL names
    `patient_name` and the sources name the table, so on a block those fields are the
    leak and must be cleared."""
    result = make_target(BODY_SQL).ask("neurology claims?", token="t")
    result.answer = "I can't share that response."
    rendered = MediBotTarget.render(result, withhold=True)

    assert set(rendered) == SHAPE
    assert rendered["sources"] == []
    assert rendered["sql"] is None
    assert rendered["refusal"] == "blocked"
    assert "patient_name" not in str(rendered)
    assert BODY_SQL["answer"] not in str(rendered)


def test_state_c_emits_a_complete_body_even_when_the_target_was_never_called():
    """An input block means there is no target response to rebuild from. Every key must
    still be present: a partial body reaches a browser as `undefined.length`."""
    rendered = MediBotTarget.render(TargetResponse(answer="I can't help with that."), withhold=True)
    assert set(rendered) == SHAPE
    assert rendered["sources"] == []


def test_render_never_reports_a_block_reason():
    """The block reason goes to the log, never the response. There is no parameter to
    pass one through."""
    import inspect

    assert "guardrail" not in inspect.signature(MediBotTarget.render).parameters
    rendered = MediBotTarget.render(TargetResponse(answer="I can't share that."), withhold=True)
    assert rendered["refusal"] == "blocked"             # a label, not a reason
    for leaky in ("PROMPT_ATTACK", "pii", "policy", "grounding", "scope"):
        assert leaky not in str(rendered)


def test_render_copies_only_known_keys():
    """A field added to the target later cannot silently start reaching browsers."""
    result = make_target({**BODY_TODAY, "internal_debug": "secret"}).ask("q", token="t")
    assert "internal_debug" not in MediBotTarget.render(result)


# --- non-chat passthrough ---------------------------------------------------
def test_forward_passes_a_login_through_untouched():
    status, body = make_target(BODY_TODAY).forward(
        "POST", "/login", body={"username": "nurse.priya", "password": "nurse.priya-demo"}
    )
    assert status == 200
    assert body["token"] == "minted.by.us"


def test_forward_surfaces_transport_failure_as_TargetError():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    target = MediBotTarget(config(), client=httpx.Client(transport=httpx.MockTransport(refuse)))
    with pytest.raises(TargetError):
        target.forward("GET", "/collections/nurse")
