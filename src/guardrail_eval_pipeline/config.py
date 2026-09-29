"""Target configuration: one YAML file per system this pipeline watches.

These files live here, not in the target's repository. The pipeline decides what a correct
answer is, so the questions, expected answers and thresholds cannot sit beside the system
being judged by them.

    the target knows how to be asked        -> the adapter, which is code
    the pipeline decides what correct means -> these files

Only keys with a reader today are parsed. `capabilities:`, `guardrails:` and `evaluation:`
are written in the YAML already but stay in `raw` until the step that needs them.
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
