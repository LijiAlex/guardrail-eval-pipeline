#!/usr/bin/env bash
#
# Start the target and this pipeline together, and wait until both answer.
#
# The pipeline is useless without something in front of it to watch, and the two have to
# come up in order: the pipeline answers 502 until the target is reachable. Doing that by
# hand means two terminals and two paths that both contain a space.
#
#   ./run.sh              start both
#   ./run.sh --eval       also ask the target for the passages it retrieved
#
# Ctrl-C stops both. The frontend is not started here, because its output is chatty and
# mixing three logs in one terminal helps nobody; the command for it is printed at the end.
#
# Written for bash 3.2, which is what macOS ships, so no negative array indices.

set -euo pipefail

PIPELINE_PORT="${GEP_PORT:-9000}"
TARGET_PORT="${MEDIBOT_PORT:-8000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_HOME="${MEDIBOT_HOME:-$(cd "$HERE/.." && pwd)/Assignment 2 medibot}"

EXPOSE_EVAL=false
if [ "${1:-}" = "--eval" ]; then EXPOSE_EVAL=true; fi

log() { printf '  %s\n' "$*"; }
die() { printf '\n  %s\n\n' "$*" >&2; exit 1; }

# Checks that produce a clear message here rather than a confusing failure later.
[ -d "$TARGET_HOME/backend" ] || die "No target at: $TARGET_HOME
  Set MEDIBOT_HOME to the directory holding the MediBot repository."

for venv in "$TARGET_HOME/backend/.venv" "$HERE/.venv"; do
  [ -x "$venv/bin/python" ] || die "No environment at $venv
  Run 'uv sync' in that repository first."
done

for port in "$TARGET_PORT" "$PIPELINE_PORT"; do
  if lsof -ti:"$port" >/dev/null 2>&1; then
    die "Port $port is already in use. Stop what is on it, or set
  MEDIBOT_PORT / GEP_PORT to something else."
  fi
done

# The target keeps an embedded vector store that allows one process at a time, so its own
# test suite and its server cannot both run. Worth saying now rather than as an opaque
# lock error thirty seconds in.
if pgrep -f "[p]ytest" >/dev/null 2>&1; then
  log "note: pytest is running somewhere. If that is the target's suite, this will fail"
  log "      to open its vector store, which allows one process at a time."
fi

TARGET_LOG=/tmp/medibot.log
PIPELINE_LOG=/tmp/guardrail-pipeline.log
TARGET_PID=
PIPELINE_PID=

cleanup() {
  printf '\n  stopping\n'
  for pid in "$TARGET_PID" "$PIPELINE_PID"; do
    if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; fi
  done
  wait 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

# url, name, pid, log path, seconds
wait_for() {
  local url=$1 name=$2 pid=$3 logfile=$4 limit=$5 waited=0
  while ! curl -fsS -m 2 "$url" >/dev/null 2>&1; do
    sleep 1
    waited=$((waited + 1))
    if ! kill -0 "$pid" 2>/dev/null; then
      printf '\n  %s exited. Last lines of %s:\n\n' "$name" "$logfile" >&2
      tail -15 "$logfile" >&2
      exit 1
    fi
    if [ "$waited" -ge "$limit" ]; then
      die "$name did not answer within ${limit}s. See $logfile"
    fi
  done
  log "$name ready after ${waited}s"
}

printf '\n'
log "target   : $TARGET_HOME"
if [ "$EXPOSE_EVAL" = true ]; then
  log "passages : exposed, so the pipeline can see what the target retrieved"
else
  log "passages : not exposed (pass --eval to turn them on)"
fi
printf '\n'

# Run from the backend directory: the target resolves its vector store relative to it.
log "starting the target on :$TARGET_PORT ..."
(
  cd "$TARGET_HOME/backend"
  MEDIBOT_EXPOSE_EVAL="$EXPOSE_EVAL" exec .venv/bin/python -m uvicorn \
    medibot.api.app:app --port "$TARGET_PORT"
) >"$TARGET_LOG" 2>&1 &
TARGET_PID=$!
# It loads an embedding model and a cross-encoder on the way up, slow from cold.
wait_for "http://localhost:$TARGET_PORT/health" "target" "$TARGET_PID" "$TARGET_LOG" 120

log "starting the pipeline on :$PIPELINE_PORT ..."
(
  cd "$HERE"
  exec .venv/bin/python -m uvicorn \
    guardrail_eval_pipeline.api.app:app --port "$PIPELINE_PORT"
) >"$PIPELINE_LOG" 2>&1 &
PIPELINE_PID=$!
wait_for "http://localhost:$PIPELINE_PORT/health" "pipeline" "$PIPELINE_PID" "$PIPELINE_LOG" 30

printf '\n'
curl -fsS "http://localhost:$PIPELINE_PORT/health" | sed 's/^/  /'
printf '\n\n'
log "Point the UI at the pipeline:"
printf '\n'
printf '      cd %q/frontend && NEXT_PUBLIC_API_URL=http://localhost:%s pnpm dev\n' \
  "$TARGET_HOME" "$PIPELINE_PORT"
printf '\n'
log "logs: $TARGET_LOG"
log "      $PIPELINE_LOG"
log "Ctrl-C stops both."
printf '\n'

wait
