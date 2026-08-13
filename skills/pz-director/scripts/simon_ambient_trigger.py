#!/usr/bin/env python3
"""Trigger gate for the SIMON Ambient Director cron job.

Returns {"fire": true} only if listener state shows recent player activity.
Reads state/discord-message-state.json only — never posts to Discord,
never invokes pz-console.sh, never makes any LLM/network calls.

The listener (simon_fast_listener.py) writes this state file whenever it
sees a relay-bot message in either #pz-molt or #pz-molt-commands. The
trigger inspects the cached response to decide whether SIMON should
wake up this tick.

Detection rules (any one fires):
  1. Recent "connected to server" event (someone joined)
  2. Recent "Players connected (N)" with N >= 1 (anyone online)

Otherwise: skip. Empty server = no LLM cost.

Side-effects before the fire decision: this trigger also performs
arc-engine housekeeping on EVERY tick (including the not-firing path):
  - syncs the active arc's playersOnline/lastPlayerLeftTs from the
    listener cache (so the 30-min stale-arc cleanup rule is grounded
    in real rosters)
  - finalizes orphan arcs (no players for >30 min) via cleanup_stale_arc

Housekeeping runs even when the trigger returns false; orphan arcs get
cleaned up regardless of whether the LLM payload fires.

Designed for use as a cron "trigger script" — see openclaw.json cron schema.
The script exits 0 with JSON on stdout. Exit codes non-zero are treated
as "skip" by the runtime.
"""
import json
import re
import sys
import time
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "discord-message-state.json"
MAX_AGE_SECONDS = 900  # 15 minutes — within one cron tick window


def emit(fire: bool) -> None:
    """Print result JSON and exit cleanly."""
    print(json.dumps({"fire": fire}))
    sys.exit(0)


def main() -> None:
    # ---- Arc-engine housekeeping (runs every tick, even when not firing) ----
    # We import lazily so a missing helper doesn't kill the cron — we just
    # skip housekeeping in that case.
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import simon_arc_engine  # noqa: F401
    except Exception as _imp:
        simon_arc_engine = None

    last_resp = ""

    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            last_resp = str(state.get("last_relay_response", "") or "")
            last_ts = int(state.get("last_check_ts", 0) or 0)
        except (json.JSONDecodeError, OSError):
            last_ts = 0
    else:
        last_ts = 0

    if simon_arc_engine:
        try:
            simon_arc_engine.sync_players_online_from_text(last_resp)
        except Exception:
            pass
        try:
            simon_arc_engine.cleanup_stale_arc()
        except Exception:
            pass

    # ---- Fire-decision (only when listener cache is fresh) ----
    now = int(time.time())
    if last_ts == 0 or (now - last_ts) > MAX_AGE_SECONDS:
        emit(False)

    # Rule 1: someone just joined
    if "connected to server" in last_resp:
        emit(True)

    # Rule 2: anyone currently online per the last players query
    m = re.search(r"Players connected \((\d+)\)", last_resp)
    if m and int(m.group(1)) >= 1:
        emit(True)

    # No evidence of activity — skip the LLM call
    emit(False)


if __name__ == "__main__":
    main()