"""A guardrail, observability and evaluation layer that wraps a chatbot from the outside.

`.env` is loaded here, at import, because every entry point needs it — the API, the
evaluation runner, and the inline example. Values already in the environment win, so
`run.sh` and CI can override.

The LangSmith project is NOT decided here. It belongs to the system being watched, not to
the watcher, and is derived from the target config so the pipeline's spans land beside the
target's own. See `TargetConfig.project`.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# An explicit path, not `load_dotenv()` on its own. Without one, python-dotenv locates the
# file by walking up from the *calling* file — which is this package, not the repository —
# and the result depends on where the process was started from.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
