"""Adapter registry. The `adapter:` key in a target config resolves to a class here.

A registered name rather than an import path, so a config file cannot name arbitrary code
to import. Adding a system means adding a module and one line to `ADAPTERS`.
"""

from __future__ import annotations

from guardrail_eval_pipeline.config import TargetConfig
from guardrail_eval_pipeline.contracts import Target, TargetError, implements_target
from guardrail_eval_pipeline.adapters.medibot import MediBotTarget
from guardrail_eval_pipeline.adapters.stub import StubTarget, sample_answer

ADAPTERS = {"medibot": MediBotTarget}

__all__ = ["ADAPTERS", "MediBotTarget", "StubTarget", "Target", "TargetError", "build_target", "sample_answer"]


def build_target(config: TargetConfig) -> Target:
    """Build the adapter that a target config names, and check it satisfies the contract.

    Raises ValueError when the name is not registered, and TypeError when the class it
    resolves to does not implement `Target` properly.
    """
    if config.adapter not in ADAPTERS:
        known = ", ".join(sorted(ADAPTERS))
        raise ValueError(f"unknown adapter {config.adapter!r} (known: {known})")
    target = ADAPTERS[config.adapter](config)
    if not implements_target(target):
        # Checked at build time; otherwise a wrong `ask` signature surfaces mid-request.
        raise TypeError(f"adapter {config.adapter!r} does not satisfy the Target contract")
    return target
