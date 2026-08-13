#!/usr/bin/env python3
"""Trigger gate for the SIMON Greeting Dispatcher cron job.

Returns {"fire": true} only if state/greeting-queue.json has pending entries.
No Discord posts, no LLM calls — just a file read.

Designed for a 1-minute cron schedule so greetings fire within ~60s of
a player connection event being queued by the listener.
"""
import json
import sys
from pathlib import Path

QUEUE_FILE = Path(__file__).resolve().parent.parent / "state" / "greeting-queue.json"


def emit(fire: bool) -> None:
    print(json.dumps({"fire": fire}))
    sys.exit(0)


def main() -> None:
    if not QUEUE_FILE.exists():
        emit(False)

    try:
        queue = json.loads(QUEUE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        emit(False)

    pending = queue.get("pending", [])
    if pending and len(pending) > 0:
        emit(True)

    emit(False)


if __name__ == "__main__":
    main()
