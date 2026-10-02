"""The input and output checks, backed by an AWS Bedrock guardrail.

Bedrock's `ApplyGuardrail` evaluates text on its own without invoking a model, which is
what lets this layer sit outside the target rather than inside it: no model is called, no
target credentials are needed, and the target does not have to know it is being watched.

The verdict is a typed enum plus a list of policies that fired, never a sentence to be
pattern-matched.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any

from langsmith import traceable

from guardrail_eval_pipeline.config import GuardrailPolicy
from guardrail_eval_pipeline.contracts import TargetResponse, Verdict
from guardrail_eval_pipeline.guardrails import deterministic

CITATION_MARKER = re.compile(r"【[^】]*】")


def normalise(text: str) -> str:
    """Strip citation markers, then fold unicode punctuation.

    Grounding scores the answer's wording, so the target's `【1†L1-L3】` markers depress a
    correct answer badly. NFKC does not affect grounding, and is applied for the
    deterministic checks, which compare strings exactly and would miss an identifier
    written with a narrow no-break space. See the README for the measurements.
    """
    return unicodedata.normalize("NFKC", CITATION_MARKER.sub("", text))

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

    A guardrail that waits indefinitely becomes the request's latency. Short timeouts turn
    that into a fail-closed block, which is a decision rather than a hang.
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
    identifier_patterns: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_policy(cls, policy: GuardrailPolicy, *, client: Any | None = None) -> BedrockGuardrail:
        """Resolve the guardrail named in a policy file to the one deployed in Bedrock.

        By name, not by id: the id is an artefact of deployment, so storing it would make
        the policy file stop being the single source.
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
                        identifier_patterns=policy.identifier_patterns,
                    )
        raise LookupError(
            f"no guardrail named {policy.name!r} in {policy.region}. "
            f"Apply it with: python scripts/guardrail.py apply guardrails/{policy.name}.yaml"
        )

    # --- the checks -----------------------------------------------------------
    @traceable(run_type="tool", name="guardrail input")
    def check_input(self, text: str) -> Verdict:
        """Judge a question before the target sees it.

        Properties of the text alone, so no identity is needed: who is asking is settled by
        the token the target verifies.
        """
        blocks = [{"text": {"text": text, "qualifiers": ["guard_content"]}}]
        return self._apply("INPUT", blocks, self.input_message)

    @traceable(run_type="tool", name="guardrail output")
    def check_output(self, response: TargetResponse, *, question: str,
                     allowed_scopes: list[str] | None = None) -> Verdict:
        """Judge an answer before the caller sees it.

        Grounding needs the passages, the question and the answer together.

        An answer not built from passages — drawn from records, or a refusal — cannot be
        grounded, so that is reported unavailable, and nothing in it is masked: the target
        answered it without refusing, so its own gate already settled what this caller may
        see. An answer that should have carried passages and did not is the opposite case
        and blocks, so that losing the check by forgetting a flag is loud.
        """
        answer = normalise(response.answer)

        if response.grounded and not response.contexts:
            return Verdict(
                blocked=True, failed_closed=True, reasons=("contexts-missing",),
                message=self.output_message,
                detail="the answer was built from passages but none were supplied, so it "
                       "cannot be grounded",
            )

        blocks: list[dict] = []
        if response.contexts:
            blocks += [{"text": {"text": c.text, "qualifiers": ["grounding_source"]}}
                       for c in response.contexts]
            blocks.append({"text": {"text": question, "qualifiers": ["query"]}})
        blocks.append({"text": {"text": answer, "qualifiers": ["guard_content"]}})

        verdict = self._apply("OUTPUT", blocks, self.output_message)

        # The checks Bedrock cannot make. Decided by comparison, so they block outright
        # rather than contributing to a score.
        leaked = deterministic.scope_leak(response.contexts, allowed_scopes)
        uncontained = deterministic.uncontained_identifiers(
            answer, response.contexts, self.identifier_patterns)
        # Reported, never blocking: a miscounted marker is a correctness problem, not a
        # leak. The judge grades citation correctness; this records it.
        # The RAW answer: normalising strips the markers this check reads.
        citations = deterministic.bad_citations(
            response.answer, response.citations, response.contexts)

        definite = tuple(f"scope-leak:{s}" for s in leaked or ()) + tuple(uncontained or ())
        noted = tuple(citations or ())
        if definite:
            return Verdict(blocked=True, reasons=verdict.reasons + definite + noted,
                           message=self.output_message, usage=verdict.usage)
        if noted:
            verdict = replace(verdict, reasons=verdict.reasons + noted)

        if verdict.masked_text is not None and not response.contexts:
            # Masking withheld. The reasons stay, so the log records what was found.
            return Verdict(blocked=verdict.blocked, reasons=verdict.reasons,
                           message=verdict.message, masked_text=None,
                           failed_closed=verdict.failed_closed, detail="masking not applied: "
                           "the target answered from its own records for a caller it did "
                           "not refuse", usage=verdict.usage)
        return verdict

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
            # Unreachable, throttled, timed out, bad credentials: the check did not happen.
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
            # Includes a missing action: the alternative is passing text nobody judged.
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
            # Bedrock returns the configured refusal, so this repository keeps no copy.
            return Verdict(blocked=True, reasons=tuple(reasons), message=text or fallback,
                           usage=response.get("usage") or {})
        # Intervened without blocking means masked: the text stands, entities replaced.
        return Verdict(blocked=False, reasons=tuple(reasons), masked_text=text or None,
                       usage=response.get("usage") or {})
