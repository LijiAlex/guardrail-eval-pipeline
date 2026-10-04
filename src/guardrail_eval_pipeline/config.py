"""Target configuration: one YAML file per system this pipeline watches.

These files live here, not in the target's repository. The pipeline decides what a correct
answer is, so the questions, expected answers and thresholds cannot sit beside the system
being judged by them.

    the target knows how to be asked        -> the adapter, which is code
    the pipeline decides what correct means -> these files

Only keys with a reader today are parsed; the rest stay in `raw` until the step that needs
them. `load_policy` reads the other kind of file — `guardrails/<target>.yaml`, which
describes the guardrail itself rather than where the target lives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when a target config file is missing, malformed, or lacks a required key."""


@dataclass
class TargetConfig:
    """One watched system: where it is, and how to authenticate as each principal.

    `principals` maps a principal to the account to log in as. **The key must be the
    principal the target itself reports** — the output guardrail compares the two, and a
    key chosen for any other reason makes that comparison meaningless.

    `raw` keeps the whole file, so a later step can read a key this class does not parse.
    """

    name: str
    adapter: str
    endpoint: str
    principals: dict[str, str] = field(default_factory=dict)
    auth: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def project(self) -> str:
        """The observability project this target's traces belong in.

        Defaults to the target's name, because a trace that crosses both processes is only
        one trace if both report to the same project — and the target already reports under
        its own name. Override with `observability.project` when it reports under another.
        """
        configured = (self.raw.get("observability") or {}).get("project")
        return configured or self.name

    @property
    def default_principal(self) -> str | None:
        """Return the first principal listed in the config, or None when none are configured.

        This is used by callers that authenticate from configuration and did not name a
        principal of their own.
        """
        return next(iter(self.principals), None)


def load_target(path: str | Path) -> TargetConfig:
    """Read one target YAML. Raises `ConfigError` on a missing file or a missing key.

    `name`, `adapter` and `endpoint` are required rather than defaulted: a defaulted
    endpoint sends every request somewhere plausible and wrong, which fails looking like a
    bad answer rather than a bad config.
    """
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"no target config at {path}")

    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")

    missing = [key for key in ("name", "adapter", "endpoint") if not data.get(key)]
    if missing:
        raise ConfigError(f"{path}: missing required key(s): {', '.join(missing)}")

    return TargetConfig(
        name=data["name"],
        adapter=data["adapter"],
        endpoint=str(data["endpoint"]).rstrip("/"),
        principals=data.get("principals") or {},
        auth=data.get("auth") or {},
        raw=data,
    )


@dataclass(frozen=True)
class GuardrailPolicy:
    """The parts of a guardrail policy file this pipeline needs at runtime.

    The file's real audience is Bedrock, via `scripts/guardrail.py`. Four values matter
    here: which guardrail to call, where it lives, and the two refusals to fall back on
    when Bedrock did not answer and so returned no message of its own.
    """

    name: str
    region: str
    input_message: str
    output_message: str
    version: str = "DRAFT"
    # name -> regex, from the file's `pii_regexes`. The same patterns Bedrock masks with,
    # reused by the containment check so the two cannot drift apart.
    identifier_patterns: dict[str, str] = field(default_factory=dict)
    # Entity types and regex names the file marks `restore_if_retrieved: true`. Those may
    # be shown unmasked when the value appears in a passage the caller actually retrieved.
    # Absent means never, so a policy that says nothing masks everything, as before.
    restorable: frozenset[str] = frozenset()
    raw: dict[str, Any] = field(default_factory=dict)


def load_policy(path: str | Path) -> GuardrailPolicy:
    """Read a guardrail policy file. Raises `ConfigError` if it is missing or incomplete."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"no guardrail policy at {path}")

    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")

    required = ("name", "region", "blocked_input_message", "blocked_output_message")
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ConfigError(f"{path}: missing required key(s): {', '.join(missing)}")

    return GuardrailPolicy(
        name=data["name"],
        region=data["region"],
        input_message=data["blocked_input_message"],
        output_message=data["blocked_output_message"],
        # DRAFT follows whatever was last applied. A deployment should pin a published
        # version so editing the policy cannot change a running system by surprise.
        version=str(data.get("version", "DRAFT")),
        identifier_patterns={
            entry["name"]: entry["pattern"]
            for entry in data.get("pii_regexes") or []
            if entry.get("name") and entry.get("pattern")
        },
        restorable=frozenset(
            str(entry.get("type") or entry.get("name"))
            for entry in (data.get("pii_entities") or []) + (data.get("pii_regexes") or [])
            if entry.get("restore_if_retrieved") and (entry.get("type") or entry.get("name"))
        ),
        raw=data,
    )
