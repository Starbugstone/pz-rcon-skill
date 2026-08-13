#!/usr/bin/env python3
"""SIMON's global server memory.

Two long-lived artifacts:
- ``server-history.json`` — chronological event log (completed arcs,
  major broadcasts, mutations, milestones). Capped at 200 events; older
  entries get archived.
- ``lore.json`` — synthesized "state of the world" narrative. SIMON pulls
  from here when greeting returning and new players so the bunker feels
  continuous, not amnesiac.

Use cases for callers
---------------------
- ``log_event(event)`` — append a server-wide event (arc completion,
  milestone, weather shift). Idempotent only by ``event_type+ts`` keys;
  callers should attach a unique ``eventId`` if they want stricter dedupe.
- ``append_lore(text_block, fact_merge=None)`` — edit the lore narrative.
- ``build_returning_player_brief()`` / ``build_new_player_brief()`` —
  read the lore + recent completed arcs and emit a paragraph that SIMON
  can deliver as a "here's what happened" recap.
- ``trim_history()`` — rotate the event log; archive older events.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
MEMORY_DIR = SKILL_DIR / "state" / "memory"
GLOBAL_DIR = MEMORY_DIR / "global"
HISTORY_FILE = GLOBAL_DIR / "server-history.json"
LORE_FILE = GLOBAL_DIR / "lore.json"
ARCHIVE_DIR = GLOBAL_DIR / "archive"

MAX_HISTORY = 200  # cap when trimming


def _read_json(path: Path, default):
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def log_event(event: dict) -> dict:
    """Append an event to server-history. Caller supplies ``ts``, ``type``,
    and event-specific fields. Returns the canonicalized event dict that
    was actually written."""
    if "ts" not in event:
        event["ts"] = int(time.time())
    if "type" not in event:
        event["type"] = "unknown"
    data = _read_json(HISTORY_FILE, {"events": [], "schemaVersion": 1})
    data.setdefault("events", []).append(event)
    data["events"] = data["events"][-MAX_HISTORY:]
    _write_json(HISTORY_FILE, data)
    return event


def trim_history(force: bool = False) -> int:
    """If history exceeds MAX_HISTORY entries (or ``force=True``), rotate
    older events into an archive file. Returns count moved."""
    data = _read_json(HISTORY_FILE, {"events": []})
    events = data.get("events", [])
    if not force and len(events) <= MAX_HISTORY:
        return 0
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    archive_path = ARCHIVE_DIR / f"history-{stamp}.json"
    moved = events[:-MAX_HISTORY] if not force else events[:max(0, len(events) - MAX_HISTORY)]
    if moved:
        _write_json(archive_path, {"archived_at": int(time.time()), "events": moved})
        data["events"] = events[-MAX_HISTORY:] if not force else events[-MAX_HISTORY:]
        _write_json(HISTORY_FILE, data)
    return len(moved)


def append_lore(narrative: str | None = None, fact_merge: dict | None = None) -> dict:
    """Update lore.json. ``narrative`` replaces the narrative field;
    ``fact_merge`` shallow-merges into ``facts``. Returns the updated doc."""
    data = _read_json(
        LORE_FILE,
        {
            "narrative": "",
            "facts": {"eraStart": "?", "totalArcsCompleted": 0, "totalServerResets": 0, "lastUpdatedTs": 0},
            "schemaVersion": 1,
        },
    )
    if narrative is not None:
        data["narrative"] = str(narrative)[:2000]
    if fact_merge:
        data.setdefault("facts", {}).update(fact_merge)
    data["facts"]["lastUpdatedTs"] = int(time.time())
    _write_json(LORE_FILE, data)
    return data


def rebuild_lore_from_history(max_arcs: int = 5) -> dict:
    """Rebuild the lore narrative from the last N completed arcs (read from
    arcs/index.json). The narrative is a short paragraph that ends with
    'Simon, out.' For a richer rebuild, callers may pass a longer paragraph
    via ``append_lore(narrative=...)``.

    Returns the updated lore document.
    """
    try:
        from simon_arc_engine import INDEX_FILE as ARCS_INDEX
        idx = _read_json(ARCS_INDEX, {"completed": []})
    except Exception:
        idx = {"completed": []}

    completed = idx.get("completed", [])[-max_arcs:]
    if not completed:
        return append_lore(
            narrative=(
                "The bunker radio has been broadcasting into static for an eternity. "
                "Nothing has come back. Simon, out."
            )
        )

    lines = ["The bunker's been busy. Quick roll-call of what came through recently:"]
    for entry in completed:
        lines.append(
            f"- {entry.get('arcName', entry.get('arcId'))}: {entry.get('summary', '')[:140]}"
        )
    lines.append("That's where we are. Simon, out.")
    return append_lore(narrative="\n".join(lines))


def build_returning_player_brief() -> str:
    """Short lore paragraph to deliver to a returning player. 1-3 sentences.
    Falls back to a generic 'radio's been quiet' line if lore is empty."""
    lore = _read_json(LORE_FILE, {"narrative": ""})
    text = (lore.get("narrative") or "").strip()
    if not text or "broadcasting into static for an eternity" in text:
        return "Radio's been quiet. Nothing new on the receiver. Simon, out."
    # Trim to first paragraph
    first = text.split("\n", 1)[0]
    return first[:500]


def build_new_player_brief() -> str:
    """Broader intro paragraph for a player who's never been on the radio.
    Adds a 'what's this place' beat to the lore paragraph."""
    lore_line = build_returning_player_brief()
    return (
        f"{lore_line} "
        "If you're picking up this frequency for the first time, you're hearing "
        "the last voice on the airwaves. Welcome, kid. Simon, out."
    )


if __name__ == "__main__":
    # Smoke: log a fake event and rebuild lore from history.
    log_event({"ts": int(time.time()), "type": "smoke_test", "details": "ok"})
    out = rebuild_lore_from_history()
    print(json.dumps(out, indent=2)[:800])
