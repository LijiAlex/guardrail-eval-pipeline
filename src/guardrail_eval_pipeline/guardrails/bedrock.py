"""The input and output checks, backed by an AWS Bedrock guardrail.

Bedrock's `ApplyGuardrail` evaluates text on its own without invoking a model, which is
what lets this layer sit outside the target rather than inside it: no model is called, no
target credentials are needed, and the target does not have to know it is being watched.

The verdict is a typed enum plus a list of policies that fired, never a sentence to be
pattern-matched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from guardrail_eval_pipeline.config import GuardrailPolicy
from guardrail_eval_pipeline.contracts import Verdict

# Where a blocked or masked entry can appear in an assessment.
_ASSESSMENT_PATHS = (
    ("topicPolicy", "topics", "name"),
    ("contentPolicy", "filters", "type"),
    ("contextualGroundingPolicy", "filters", "type"),
    ("sensitiveInformationPolicy", "piiEntities", "type"),
    ("sensitiveInformationPolicy", "regexes", "name"),
)


def _client(region: str) -> Any:
    """A Bedrock runtime client that gives up rather than hanging.

    A guardrail that waits indefinitely becomes the request's latency, and the caller
    cannot tell a slow check from a failed one. Short timeouts turn that into a
    fail-closed block, which is a decision rather than a hang.
    """
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(connect_timeout=2, read_timeout=5,
                      retries={"max_attempts": 2, "mode": "standard"}),
    )


@dataclass
class BedrockGuardrail:
    """One configured Bedrock guardrail, ready to judge text."""

    identifier: str
    version: str
    client: Any
    input_message: str
    output_message: str

    @classmethod
    def from_policy(cls, policy: GuardrailPolicy, *, client: Any | None = None) -> BedrockGuardrail:
        """Resolve the guardrail named in a policy file to the one deployed in Bedrock.

        Resolving by name rather than storing an id keeps the policy file the single
        source: the id is an artefact of deployment, the name is the thing a person wrote.
        """
        import boto3

        control = boto3.client("bedrock", region_name=policy.region)
        for page in control.get_paginator("list_guardrails").paginate():
            for summary in page["guardrails"]:
                if summary["name"] == policy.name:
                    return cls(
                        identifier=summary["id"],
                        version=policy.version,
                        client=client or _client(policy.region),
                        input_message=policy.input_message,
                        output_message=policy.output_message,
                    )
        raise LookupError(
            f"no guardrail named {policy.name!r} in {policy.region}. "
            f"Apply it with: python scripts/guardrail.py apply guardrails/{policy.name}.yaml"
        )

    # --- the checks -----------------------------------------------------------
    def check_input(self, text: str) -> Verdict:
        """Judge a question before the target sees it.

        Properties of the text alone — injection, denied topics, abuse — so this needs no
        identity. Who is asking is settled by the token the target verifies.
        """
        blocks = [{"text": {"text": text, "qualifiers": ["guard_content"]}}]
        return self._apply("INPUT", blocks, self.input_message)

    # --- the one place a Bedrock response becomes a Verdict --------------------
    def _apply(self, source: str, blocks: list[dict], fallback: str) -> Verdict:
        try:
            response = self.client.apply_guardrail(
                guardrailIdentifier=self.identifier,
                guardrailVersion=self.version,
                source=source,
                content=blocks,
            )
        except Exception as exc:  # noqa: BLE001 — any failure is the same decision
            # Unreachable, throttled, timed out, bad credentials: all mean the check did
            # not happen, and an unchecked request is not a safe one.
            return Verdict(
                blocked=True,
                failed_closed=True,
                reasons=("guardrail-unavailable",),
                message=fallback,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return self._read(response, fallback)

    @staticmethod
    def _read(response: Any, fallback: str) -> Verdict:
        """Turn a Bedrock response into a Verdict, or fail closed if it is not one."""
        if not isinstance(response, dict):
            return Verdict(blocked=True, failed_closed=True, reasons=("unreadable-verdict",),
                           message=fallback, detail=f"expected a mapping, got {type(response)}")

        action = response.get("action")
        if action == "NONE":
            return Verdict(blocked=False, usage=response.get("usage") or {})

        if action != "GUARDRAIL_INTERVENED":
            # Includes a missing action. A verdict we cannot read is treated as a block,
            # because the alternative is passing text nobody judged.
            return Verdict(blocked=True, failed_closed=True, reasons=("unrecognised-verdict",),
                           message=fallback, detail=f"action={action!r}")

        reasons, blocked = [], False
        for assessment in response.get("assessments", []):
            for group, key, label in _ASSESSMENT_PATHS:
                for entry in assessment.get(group, {}).get(key, []) or []:
                    entry_action = entry.get("action")
                    if entry_action in ("BLOCKED", "ANONYMIZED"):
                        reasons.append(f"{entry.get(label)}")
                        blocked = blocked or entry_action == "BLOCKED"

        text = "".join(part.get("text", "") for part in response.get("outputs", []))
        if blocked:
            # Bedrock returns the configured refusal here, so there is no second copy of
            # that wording in this repository.
            return Verdict(blocked=True, reasons=tuple(reasons), message=text or fallback,
                           usage=response.get("usage") or {})
        # Intervened without blocking means masked: the text stands, with entities
        # replaced. Input masking is switched off in the policy, so this cannot arise on
        # the input side today.
        return Verdict(blocked=False, reasons=tuple(reasons), masked_text=text or None,
                       usage=response.get("usage") or {})
