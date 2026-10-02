"""Test-wide guards.

The suite must be offline. Before the guardrail existed that was true by construction;
now a missed dependency override reaches AWS, which is slow, needs credentials, and tests
somebody else's service rather than this one.

So creating an AWS client inside a test is an error rather than a network call. Tests that
need a guardrail build `BedrockGuardrail` directly with a stub client, which is also the
only way to drive the fail-closed paths on purpose.
"""

from __future__ import annotations

import boto3
import pytest


@pytest.fixture(autouse=True)
def no_aws(monkeypatch):
    def refuse(*args, **kwargs):
        service = args[0] if args else kwargs.get("service_name", "?")
        raise RuntimeError(
            f"a test tried to create an AWS client ({service}). Tests run offline — "
            "override the dependency, or build BedrockGuardrail with a stub client."
        )

    monkeypatch.setattr(boto3, "client", refuse)
