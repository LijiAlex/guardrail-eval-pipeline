"""The record of what was decided, written whether or not anything is tracing.

Spans and events answer different questions. A span shows where a request spent its time
and nests one process inside another; an event is the durable record that a particular
question was blocked, by which policy, under which guardrail version. Tracing is optional,
so a verdict that existed only as a span would disappear the moment it was switched off —
and the verdict is the safety record.

One event per request, as JSON lines under `logs/<target>/events.jsonl`. Queryable with
nothing more than `jq`, which is the point: the spec asks for structured, queryable events
and explicitly does not ask for a dashboard.

Nothing here knows about HTTP or Bedrock, so the evaluation runner and the inline example
record the same way the proxy does.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# One process writes one line at a time. Short appends to a line-oriented file are atomic
# enough in practice; this removes the interleaving that a threaded server would otherwise
# produce within a single process.
_LOCK = threading.Lock()


@dataclass
class Event:
    """One request, and what the guardrails decided about it.

    `decision` is the summary a report counts: allowed, blocked, masked, or target_refused.
    Those last two matter separately — a target declining on its own terms is it behaving
    correctly, and counting that as a block would read correct behaviour as an attack.

    `guardrail` records the id and version that produced the verdict. A verdict is not
    interpretable without the configuration that produced it, and that configuration
    changes.
    """

    request_id: str
    target: str
    decision: str
    blocked_at: str | None = None          # "input", "output", or None
    reasons: list[str] = field(default_factory=list)
    failed_closed: bool = False
    principal: str | None = None
    question: str | None = None
    answer: str | None = None
    guardrail: dict[str, str] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    trace_id: str | None = None            # joins this event to its span
    detail: str | None = None
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def log_dir() -> Path:
    """Where events are written. `GEP_LOG_DIR` overrides, for a test or a container."""
    return Path(os.getenv("GEP_LOG_DIR", "logs"))


def path_for(target: str) -> Path:
    return log_dir() / target / "events.jsonl"


def write(event: Event, *, path: Path | None = None) -> Path:
    """Append one event. Returns where it went.

    Never raises: losing the log is bad, failing the request because the log could not be
    written is worse. A guardrail that stops answering when its disk fills has turned an
    observability problem into an outage.
    """
    destination = path or path_for(event.target)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(event), ensure_ascii=False)
        with _LOCK, destination.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    return destination


def read(target: str, *, path: Path | None = None) -> list[dict[str, Any]]:
    """Every event recorded for a target, oldest first.

    A line that cannot be parsed is skipped rather than fatal: a truncated final line from
    an interrupted write should not make the whole history unreadable.
    """
    destination = path or path_for(target)
    if not destination.is_file():
        return []
    events = []
    for line in destination.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
