#!/usr/bin/env python3
"""SIMON's narrative arc engine.

An *arc* is a multi-beat story that unfolds across several ambient cron ticks.
Each beat is a narration line SIMON can broadcast, optionally paired with a
mutation (a server command via ``pz-console.sh``). Beats advance on time:
``gapMin`` minutes must elapse since the last beat before the next one fires.

Arc lifecycle
-------------
1. **start_arc(arc_id)** — picks an arc from the catalog (``references/narrative-arcs.md``
   loaded via :func:`load_catalog`), writes ``state/memory/arcs/active.json``.
   Refuses to start if the previous arc finished within ``arc_reset_hours``
   (default 4h) OR if an arc is already active.

2. **advance_beat()** — called by the ambient cron tick. Returns the beat
   payload (``{narration, mutation}``) when it's time to fire, or ``None``
   to hold for another tick. Caller (the cron payload's LLM) decides what
   to do with it — emit as final reply, optionally exec the mutation, log
   the beat to history.

3. **record_player_join / record_player_leave** — updates ``playersOnline``
   on the active arc. Used by ``cleanup_stale_arc`` to detect orphan arcs.

4. **cleanup_stale_arc()** — if the active arc has had no players online for
   >30 min, gracefully finalize it (move summary to ``arcs/index.json``,
   archive full arc state, log to global server-history, delete active.json).

5. **finalize_arc(reason)** — called when the arc completes its final beat
   OR is cancelled. Same archiving behavior as cleanup.

6. **record_player_interaction(name, content)** — while an arc is active,
   log player chats to the arc memory so SIMON can answer in-character.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
MEMORY_DIR = SKILL_DIR / "state" / "memory"
ARCS_DIR = MEMORY_DIR / "arcs"
ACTIVE_FILE = ARCS_DIR / "active.json"
INDEX_FILE = ARCS_DIR / "index.json"
CATALOG_FILE = SKILL_DIR / "references" / "narrative-arcs.md"

ARC_RESET_HOURS = 4
STALE_PLAYER_MINUTES = 30
MAX_NARRATIONS = 50
MAX_INTERACTIONS = 60


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


def load_catalog() -> dict:
    """Parse the narrative-arcs.md catalog.

    Schema: a markdown file with one fenced JSON block per arc set, plus
    an ``## arcs`` section if there are multiple catalog sets. We expect
    a single JSON object ``{"arcs": [{arcId, arcName, summary, beats}, ...]}``
    inside the first fenced block we find tagged ```json. Falls back to an
    empty catalog on parse failure so the system stays quiet instead of
    crashing the cron.
    """
    if not CATALOG_FILE.exists():
        return {"arcs": []}
    text = CATALOG_FILE.read_text()
    # Find the first ```json ... ``` fenced block.
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not m:
        return {"arcs": []}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {"arcs": []}


def pick_random_arc(exclude: list[str] | None = None) -> dict | None:
    """Pick a random arc from the catalog. ``exclude`` is a list of arcIds
    to skip (e.g. recently completed). Returns None if catalog is empty."""
    import random
    catalog = load_catalog()
    pool = [a for a in catalog.get("arcs", []) if a.get("arcId") not in (exclude or [])]
    if not pool:
        # fall back to the full catalog if everything was excluded
        pool = catalog.get("arcs", [])
    return random.choice(pool) if pool else None


def get_active_arc() -> dict | None:
    return _read_json(ACTIVE_FILE, None)


def recent_completed_within_hours(hours: int) -> bool:
    """True if an arc completed in the last ``hours``."""
    idx = _read_json(INDEX_FILE, {"completed": []})
    cutoff = int(time.time()) - (hours * 3600)
    for entry in idx.get("completed", []):
        if entry.get("ts", 0) >= cutoff:
            return True
    return False


def start_arc(arc_id: str | None = None) -> dict | None:
    """Start an arc. Returns the new active state, or None if refused."""
    if get_active_arc():
        return None  # already active
    if recent_completed_within_hours(ARC_RESET_HOURS):
        return None  # cooldown
    if arc_id:
        catalog = load_catalog()
        chosen = next((a for a in catalog.get("arcs", []) if a.get("arcId") == arc_id), None)
        if not chosen:
            return None
    else:
        # avoid re-running the most recent completed arc immediately
        idx = _read_json(INDEX_FILE, {"completed": []})
        last_id = (idx.get("completed", []) or [{}])[-1].get("arcId") if idx.get("completed") else None
        chosen = pick_random_arc(exclude=[last_id] if last_id else [])
        if not chosen:
            return None

    now = int(time.time())
    state = {
        "schemaVersion": 1,
        "arcId": chosen["arcId"],
        "arcName": chosen.get("arcName", chosen["arcId"]),
        "summary": chosen.get("summary", ""),
        "arcStartedTs": now,
        "currentBeatIdx": 0,
        "beats": chosen.get("beats", []),
        "beatHistory": [],
        "lastBeatTs": now,  # beat 0 can fire immediately
        "playersOnline": [],
        "lastPlayerLeftTs": None,
        "narrations": [],
        "playerInteractions": [],
    }
    _write_json(ACTIVE_FILE, state)
    return state


def record_player_join(name: str) -> dict | None:
    arc = get_active_arc()
    if not arc:
        return None
    online = arc.setdefault("playersOnline", [])
    if name not in online:
        online.append(name)
        arc["lastPlayerLeftTs"] = None
        _write_json(ACTIVE_FILE, arc)
    return arc


def record_player_leave(name: str) -> dict | None:
    arc = get_active_arc()
    if not arc:
        return None
    online = arc.setdefault("playersOnline", [])
    if name in online:
        online.remove(name)
    arc["lastPlayerLeftTs"] = int(time.time())
    _write_json(ACTIVE_FILE, arc)
    return arc


_PLAYER_LIST_LINE = re.compile(
    r"Players\s+connected\s+\((\d+)\)\s*:?\s*((?:\n?[A-Za-z0-9_\- ,]{1,64})*)",
    re.IGNORECASE,
)


def sync_players_online_from_text(text: str) -> dict | None:
    """Given a relay-bot response like
    ``Players connected (3):\\nStone, Sarah, Mike``, update the active arc's
    ``playersOnline`` + ``lastPlayerLeftTs``. Names present in the prior
    state but missing from this list are treated as leaves (and a leave
    timestamp is recorded so the 30-min cleanup rule can fire).

    Called by the listener when it sees a relay-bot response in
    ``#pz-molt-commands``. Idempotent and side-effect-free when the text
    doesn't match the ``Players connected (N):`` pattern.
    """
    if not text:
        return None
    arc = get_active_arc()
    if not arc:
        return None
    m = _PLAYER_LIST_LINE.search(text)
    if not m:
        return None
    raw_names = m.group(2) or ""
    # Split on comma/newline, strip, drop empties. PZ-style names can
    # have spaces but no commas.
    names = [n.strip() for n in re.split(r"[,\n]", raw_names) if n.strip()]
    prior_online = set(arc.get("playersOnline") or [])
    new_online = set(names)
    joined = sorted(new_online - prior_online)
    left = sorted(prior_online - new_online)

    arc["playersOnline"] = sorted(new_online)
    if left:
        arc["lastPlayerLeftTs"] = int(time.time())
    elif new_online:
        # Players are present again — clear any staleness timer.
        arc["lastPlayerLeftTs"] = None
    _write_json(ACTIVE_FILE, arc)
    return {"joined": joined, "left": left, "online": arc["playersOnline"]}


def record_player_interaction(name: str, content: str, simon_reply_summary: str = "") -> dict | None:
    arc = get_active_arc()
    if not arc:
        return None
    arc.setdefault("playerInteractions", []).append(
        {
            "ts": int(time.time()),
            "player": name,
            "content": str(content)[:300],
            "simon_reply_summary": str(simon_reply_summary)[:200],
        }
    )
    arc["playerInteractions"] = arc["playerInteractions"][-MAX_INTERACTIONS:]
    _write_json(ACTIVE_FILE, arc)
    return arc


def active_arc_brief_for_player(name: str) -> str:
    """Return a short arc-context paragraph for an LLM prompt when responding
    to a player while an arc is active. Empty string if no arc."""
    arc = get_active_arc()
    if not arc:
        return ""
    beats = arc.get("beats", [])
    idx = arc.get("currentBeatIdx", 0)
    beat = beats[idx] if 0 <= idx < len(beats) else None
    last_beat_text = arc.get("narrations", [])[-1][:160] if arc.get("narrations") else "(none yet)"
    next_beat_narration = beat.get("narration", "") if beat else "(arc complete)"
    interactions = [pi for pi in arc.get("playerInteractions", []) if pi.get("player") == name]
    interaction_hint = ""
    if interactions:
        last = interactions[-1]
        interaction_hint = f" You last asked: '{last.get('content', '')[:80]}'. SIMON answered: '{last.get('simon_reply_summary', '')[:120]}'."
    return (
        f"Active arc: {arc.get('arcName', arc.get('arcId', '?'))}. "
        f"Beat {idx + 1} of {len(beats)}. "
        f"Latest SIMON narration: '{last_beat_text}'. "
        f"Planned next beat: '{next_beat_narration}'.{interaction_hint}"
    )


def advance_beat() -> dict | None:
    """Called by the ambient cron tick. Returns the beat payload when it's
    time to fire, or None when the engine wants to hold.

    A beat fires when ``now - lastBeatTs >= beats[currentBeatIdx].gapMin * 60``.
    Note: the cron payload's LLM may still hold or rewrite the narration;
    this method just decides *mechanical* readiness, not narrative taste.
    """
    arc = get_active_arc()
    if not arc:
        return None
    beats = arc.get("beats", [])
    idx = arc.get("currentBeatIdx", 0)
    if idx >= len(beats):
        # arc is past the end — let finalize_arc handle it
        finalize_arc("completed")
        return None
    beat = beats[idx]
    now = int(time.time())
    gap_sec = max(0, int(beat.get("gapMin", 0)) * 60)
    if (now - int(arc.get("lastBeatTs", 0))) < gap_sec:
        return None  # not yet

    return {
        "arcId": arc["arcId"],
        "arcName": arc.get("arcName", arc["arcId"]),
        "beatIdx": idx,
        "narration": beat.get("narration", ""),
        "mutation": beat.get("mutation"),  # may be None
        "totalBeats": len(beats),
    }


def mark_beat_fired(payload: dict, simon_narration: str = "") -> dict | None:
    """Called by the cron payload after it actually broadcasts the beat and
    (optionally) executes the mutation. Records to history, advances idx,
    appends narration to arc.narrations[], and bumps lastBeatTs."""
    arc = get_active_arc()
    if not arc:
        return None
    now = int(time.time())
    arc.setdefault("beatHistory", []).append(
        {
            "ts": now,
            "beatIdx": payload.get("beatIdx"),
            "narration_planned": payload.get("narration", ""),
            "narration_actual": simon_narration[:600] if simon_narration else payload.get("narration", ""),
            "mutation_executed": bool(payload.get("mutation_executed")),
            "mutation": payload.get("mutation"),
        }
    )
    arc.setdefault("narrations", []).append(simon_narration or payload.get("narration", ""))
    arc["narrations"] = arc["narrations"][-MAX_NARRATIONS:]
    arc["currentBeatIdx"] = int(arc.get("currentBeatIdx", 0)) + 1
    arc["lastBeatTs"] = now
    _write_json(ACTIVE_FILE, arc)

    # Auto-finalize if past the last beat
    if arc["currentBeatIdx"] >= len(arc.get("beats", [])):
        finalize_arc("completed")
    return arc


def cleanup_stale_arc() -> dict | None:
    """If the active arc has had no players online for >30 min, finalize it
    as 'abandoned' (still keep a summary in the global lore)."""
    arc = get_active_arc()
    if not arc:
        return None
    online = arc.get("playersOnline") or []
    last_left = arc.get("lastPlayerLeftTs")
    if online:
        return None  # players present — arc lives
    if not last_left:
        return None  # never had anyone — let the cron start_arc gate handle this
    now = int(time.time())
    if (now - int(last_left)) >= (STALE_PLAYER_MINUTES * 60):
        return finalize_arc("abandoned_no_players_30m")
    return None


def finalize_arc(reason: str = "completed") -> dict | None:
    """Move the active arc to the completed index, archive full state to
    archive/, log a summary to global server-history, delete active.json."""
    arc = get_active_arc()
    if not arc:
        return None
    now = int(time.time())
    beats = arc.get("beats", [])
    summary = {
        "arcId": arc.get("arcId"),
        "arcName": arc.get("arcName"),
        "summary": arc.get("summary", ""),
        "ts": now,
        "tsStart": arc.get("arcStartedTs", now),
        "durationMin": (now - int(arc.get("arcStartedTs", now))) // 60,
        "beatsFired": len(arc.get("beatHistory", [])),
        "totalBeats": len(beats),
        "playersInvolved": sorted({pi.get("player") for pi in arc.get("playerInteractions", []) if pi.get("player")}),
        "narrations": arc.get("narrations", [])[-8:],
        "reason": reason,
    }

    # 1. Append to arcs/index.json
    idx = _read_json(INDEX_FILE, {"completed": []})
    idx.setdefault("completed", []).append(
        {
            "arcId": summary["arcId"],
            "arcName": summary["arcName"],
            "ts": summary["ts"],
            "durationMin": summary["durationMin"],
            "reason": summary["reason"],
            "summary": arc.get("summary", ""),
            "playersInvolved": summary["playersInvolved"],
        }
    )
    # Cap the index to the last 50 completed arcs
    idx["completed"] = idx["completed"][-50:]
    _write_json(INDEX_FILE, idx)

    # 2. Archive full arc state
    archive_dir = ARCS_DIR / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    arc_stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(summary["ts"]))
    archive_path = archive_dir / f"{arc.get('arcId', 'arc')}-{arc_stamp}.json"
    _write_json(archive_path, {"finalized_reason": reason, "arc_state": arc, "summary": summary})

    # 3. Log to global server-history
    try:
        from simon_global_memory import log_event
        log_event(
            {
                "ts": now,
                "type": f"arc_{reason}",
                "arcId": summary["arcId"],
                "arcName": summary["arcName"],
                "durationMin": summary["durationMin"],
                "playersInvolved": summary["playersInvolved"],
                "summary": arc.get("summary", ""),
                "last_narration": (arc.get("narrations") or [""])[-1][:280],
            }
        )
    except Exception:
        # Don't fail finalize if the global memory module is unavailable
        pass

    # 4. For each player who was involved, save a short arc recap into
    #    their per-player memory so they get a callback next time.
    try:
        from simon_player_memory import append_arc_recap
        for player in summary["playersInvolved"]:
            try:
                append_arc_recap(
                    player,
                    arc.get("arcId", "?"),
                    f"survived '{arc.get('arcName', arc.get('arcId'))}' ({reason}) — {arc.get('summary', '')[:120]}",
                )
            except Exception:
                continue
    except Exception:
        pass

    # 5. Delete active.json
    if ACTIVE_FILE.exists():
        ACTIVE_FILE.unlink()

    return summary


def reset_all_arc_memory() -> int:
    """Hard-reset: delete active.json + clear the completed index. Used by
    simon_reset_world.py."""
    removed = 0
    if ACTIVE_FILE.exists():
        ACTIVE_FILE.unlink()
        removed += 1
    if INDEX_FILE.exists():
        _write_json(INDEX_FILE, {"completed": [], "schemaVersion": 1})
        removed += 1
    return removed


if __name__ == "__main__":
    import json
    # Quick listing of catalog arcs so operators can preview.
    print(json.dumps(load_catalog(), indent=2)[:1200])
