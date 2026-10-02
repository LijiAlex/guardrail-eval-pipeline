"""MediBot adapter. The only file that knows MediBot's endpoints and field names.

Translates in both directions:

    MediBot JSON   -> TargetResponse   _translate()   for the guardrails and evaluators
    TargetResponse -> MediBot JSON     render()       so MediBot's own UI keeps working

MediBot reads the role from a signed token, so `principal` selects which account to log in
as rather than becoming a request field. A caller's own token is forwarded untouched and
MediBot verifies it; the role it reports back is the authoritative one.
"""

from __future__ import annotations

import os

import httpx

from guardrail_eval_pipeline.config import TargetConfig
from guardrail_eval_pipeline.contracts import Retrieved, TargetAuthError, TargetError, TargetResponse

# The keys `render` copies. An allowlist, so a field added to MediBot later does not
# start reaching browsers through here.
PASSTHROUGH_KEYS = ("sources", "retrieval_type", "role", "sql", "refusal")


class MediBotTarget:
    """Talks to a running MediBot over HTTP.

    `client` is injectable so tests can drive the translation through a fake transport
    with no server.
    """

    name = "medibot"

    def __init__(self, config: TargetConfig, client: httpx.Client | None = None) -> None:
        self.config = config
        self._client = client or httpx.Client(timeout=60.0)
        self._tokens: dict[str, str] = {}

    def close(self) -> None:
        """Close the underlying HTTP client and release its connection pool."""
        self._client.close()

    # --- transport ----------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Send one request to the target, converting any network failure into a `TargetError`.

        Every call the adapter makes goes through here, so a caller never sees a raw
        transport exception.
        """
        try:
            return self._client.request(method, f"{self.config.endpoint}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise TargetError(f"cannot reach the target at {self.config.endpoint}: {exc}") from exc

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        """Parse a response body as a JSON object, or raise a `TargetError`.

        A target that answers with HTML or a bare list is treated as unusable rather than
        allowed to fail later with a confusing attribute error.
        """
        try:
            body = response.json()
        except ValueError as exc:
            raise TargetError(f"target returned a non-JSON body (HTTP {response.status_code})") from exc
        if not isinstance(body, dict):
            raise TargetError(f"target returned {type(body).__name__}, expected an object")
        return body

    # --- auth ---------------------------------------------------------------
    def _password(self, username: str) -> str:
        """The password for a demo account: the username plus a configured suffix.

        From `auth.password_suffix`, or `MEDIBOT_PASSWORD_SUFFIX` if set. A target with
        real accounts would read a secret store instead.
        """
        suffix = os.getenv("MEDIBOT_PASSWORD_SUFFIX") or self.config.auth.get("password_suffix", "")
        return f"{username}{suffix}"

    def _token(self, principal: str, *, refresh: bool = False) -> str:
        """Return a token for this principal, logging in if one is not already cached.

        Pass `refresh=True` to discard the cached token and authenticate again, which is
        what happens when the target rejects a token it previously issued.
        """
        if not refresh and principal in self._tokens:
            return self._tokens[principal]

        username = self.config.principals.get(principal)
        if not username:
            # The roster is left out on purpose: this message reaches an API caller.
            raise TargetError(f"no account configured for principal {principal!r}")

        response = self._request(
            "POST", "/login", json={"username": username, "password": self._password(username)}
        )
        if response.status_code != 200:
            # Status only: the body can echo the credential back.
            raise TargetError(f"login failed for {principal!r}: HTTP {response.status_code}")

        token = self._json(response).get("token")
        if not token:
            raise TargetError(f"login for {principal!r} returned no token")
        self._tokens[principal] = token
        return token

    # --- ask ----------------------------------------------------------------
    def ask(
        self,
        question: str,
        *,
        principal: str | None = None,
        token: str | None = None,
        trace_headers: dict[str, str] | None = None,
    ) -> TargetResponse:
        """Ask MediBot one question and return its answer, normalised.

        Pass `token` to forward a caller's own credential, or `principal` to authenticate
        from configuration. Raises `TargetAuthError` when a caller's credential is
        rejected, and `TargetError` for anything else that goes wrong.
        """
        if token:
            # A caller's own credential wins, and `principal` is ignored: trusting a body
            # field next to a signed token would let any user answer as any role.
            return self._ask_with(token, question, trace_headers)

        principal = principal if principal is not None else self.config.default_principal
        if not principal:
            # `not principal` rather than `is None`: an empty string must not fall through
            # to the first configured account.
            raise TargetError("a principal is required; none given and none configured")

        response = self._ask_with(self._token(principal), question, trace_headers, _raw=True)
        if response.status_code == 401:
            # A cached token can be invalidated by a target restart. Re-login once — only
            # possible for tokens we minted, not for a caller's own.
            response = self._ask_with(
                self._token(principal, refresh=True), question, trace_headers, _raw=True
            )
        return self._finish(response)

    def _ask_with(self, token, question, trace_headers, *, _raw: bool = False):
        """Post the question using a given token.

        Returns the raw response when `_raw` is set, so the caller can inspect the status
        before deciding whether to retry; otherwise returns a translated `TargetResponse`.
        """
        headers = {"Authorization": f"Bearer {token}", **(trace_headers or {})}
        response = self._request("POST", "/chat", json={"question": question}, headers=headers)
        return response if _raw else self._finish(response)

    def _finish(self, response: httpx.Response) -> TargetResponse:
        """Turn a chat response into a `TargetResponse`, or raise for a non-200 status."""
        if response.status_code == 401:
            # Reached only with a caller's own token; the minted path retries first.
            raise TargetAuthError("the target rejected this credential")
        if response.status_code != 200:
            raise TargetError(f"chat failed: HTTP {response.status_code}")
        return self._translate(self._json(response))

    # --- non-chat passthrough ------------------------------------------------
    def forward(
        self, method: str, path: str, *, body: dict | None = None, headers: dict | None = None
    ) -> tuple[int, dict]:
        """Forward a non-chat request to the target and return its (status, body).

        Used for requests that are not questions, such as a login or a capability
        lookup, so no guardrail applies to them.
        """
        response = self._request(method, path, json=body, headers=headers or {})
        return response.status_code, self._json(response)

    # --- what this principal may read ----------------------------------------
    def allowed_scopes(self, principal: str) -> list[str] | None:
        """The access zones this principal may read, as MediBot itself reports them.

        Asked rather than copied: a second copy of its role-to-collection matrix would be
        two copies of one policy with nothing to detect drift between them.

        None when it will not say. Honest limit: this checks the answer against the policy
        the target publishes, so it catches a filter bug, not a lying target.
        """
        try:
            status, body = self.forward("GET", f"/collections/{principal}")
        except TargetError:
            return None
        if status != 200:
            return None
        scopes = body.get("collections")
        return list(scopes) if isinstance(scopes, list) else None

    # --- translation --------------------------------------------------------
    @staticmethod
    def _translate(body: dict) -> TargetResponse:
        """MediBot's JSON -> `TargetResponse`.

        `refusal_reason` keeps MediBot's own code ("role", "not_found", "no_query"):
        declining on permission grounds and finding nothing are different behaviours, and
        the heuristics tell them apart.
        """
        envelope = body.get("eval") or {}

        contexts = [
            Retrieved(
                text=item.get("text", ""),
                score=item.get("score"),
                label=" / ".join(
                    part for part in (item.get("source_document"), item.get("section_title")) if part
                )
                or None,
                # MediBot's "collection" is its access zone.
                scope=item.get("collection"),
            )
            for item in envelope.get("contexts") or []
            if item.get("text")
        ]

        citations = [
            source["source_document"]
            for source in body.get("sources") or []
            if source.get("source_document")
        ]

        return TargetResponse(
            answer=body.get("answer", ""),
            contexts=contexts,
            citations=citations,
            refused=body.get("refusal") is not None,
            refusal_reason=body.get("refusal"),
            principal=body.get("role"),
            # MediBot answers documents from passages and analytics from SQL rows. Only the
            # first kind can be checked against what was retrieved.
            grounded=body.get("retrieval_type") == "hybrid_rag" and body.get("refusal") is None,
            tokens=envelope.get("tokens"),
            timings=envelope.get("timings"),
            raw=body,
        )

    @staticmethod
    def render(result: TargetResponse, *, withhold: bool = False) -> dict:
        """`TargetResponse` -> MediBot's JSON, so MediBot's own UI keeps working.

        Three states:

            withhold=False, target answered   its metadata passes through
            withhold=False, target refused    its own message and reason code survive
            withhold=True                     generic answer, sources=[] and sql=None

        Pass `withhold=True` when the GUARDRAILS blocked. Do not derive it from
        `result.refused`, which is also true when the target itself declined — those
        refusals are correct and keep their specific message.

        Withholding clears `sources` and `sql` because on a PII or scope block those
        fields are the leak: the SQL naming `patient_name`, or the zone the principal
        could not read.

        Every key is always present, including when `raw` is empty because the target was
        never called. A partial body reaches a browser as `undefined.length`.

        `eval` is never copied — it is this pipeline's envelope and holds every retrieved
        passage in full. Only the keys in `PASSTHROUGH_KEYS` are copied.
        """
        body = result.raw or {}
        if withhold:
            return {
                "answer": result.answer,
                "sources": [],
                "retrieval_type": body.get("retrieval_type"),
                "role": result.principal,
                "sql": None,
                "refusal": "blocked",
            }
        return {**{key: body.get(key) for key in PASSTHROUGH_KEYS}, "answer": result.answer}
