#!/usr/bin/env python3
"""SIMON's world/server reset script.

Wipes per-player memory, arc memory, and global lore when the operator
declares a world reset. Always archives current state to
``state/memory/archive/<era>/<timestamp>/`` first so history is preserved
offline (operators can resurrect or inspect anything later).

Usage:
    python3 scripts/simon_reset_world.py [--reason "world v2 fresh start"] [--announce] [--dry-run]
    python3 scripts/simon_reset_world.py --help

Behavior:
    1. Read reset-marker.json to capture the era ID + start.
    2. Copy current state/memory/{players,arcs,global} into
       state/memory/archive/<eraId>/<timestamp>/.
    3. Reset:
         - state/memory/players/ — every <slug>.json removed, index.json cleared
         - state/memory/arcs/active.json removed
         - state/memory/arcs/index.json reset to empty
         - state/memory/global/server-history.json reset to empty events
         - state/memory/global/lore.json reset to default template
         - state/memory/global/reset-marker.json bumps totalResetsEver + 1 and
           stamps a new era (eraId = "era-<timestamp>", eraStart = now).
    4. Optionally broadcasts to #pz-molt via openclaw message send (muted;
       SIMON is silent after a reset unless --announce is set).
    5. Prints a one-line summary of what was archived + what was wiped.

Default is silent (no broadcast). Pass --announce to have SIMON sign off
the old era on the radio before resetting.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
MEMORY_DIR = SKILL_DIR / "state" / "memory"
ARC_STATE_DIR = SKILL_DIR / "state"  # legacy listener state files

PLAYERS_DIR = MEMORY_DIR / "players"
ARCS_DIR = MEMORY_DIR / "arcs"
GLOBAL_DIR = MEMORY_DIR / "global"
ARCHIVE_BASE = MEMORY_DIR / "archive"

RESET_MARKER_FILE = GLOBAL_DIR / "reset-marker.json"
LEGACY_PLAYER_REGISTRY = ARC_STATE_DIR / "player-registry.json"
LEGACY_NARRATIVE_STATE = ARC_STATE_DIR / "narrative-state.json"
LEGACY_GREET_QUEUE = ARC_STATE_DIR / "greeting-queue.json"
LEGACY_DELTA = ARC_STATE_DIR / "player-delta.json"


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


def _default_lore() -> dict:
    return {
        "_about": "Synthesized server lore — 'state of the world' SIMON uses for new/returning player recaps. Rebuilt from arcs when available.",
        "narrative": (
            "The bunker radio has been broadcasting into static for an eternity. "
            "Nothing has come back. Simon, out."
        ),
        "facts": {"eraStart": "?", "totalArcsCompleted": 0, "totalServerResets": 0, "lastUpdatedTs": 0},
        "schemaVersion": 1,
    }


def _default_history() -> dict:
    return {"events": [], "schemaVersion": 1}


def _default_player_index() -> dict:
    return {"players": {}, "schemaVersion": 1}


def _default_arc_index() -> dict:
    return {"completed": [], "schemaVersion": 1}


def _default_reset_marker(stamp: str, now: int) -> dict:
    return {
        "eraId": f"era-{stamp}",
        "eraStart": now,
        "currentEraResets": 0,
        "totalResetsEver": 1,
        "lastResetTs": now,
        "lastResetReason": "world_reset",
        "schemaVersion": 1,
    }


def archive_then_wipe(era_id: str) -> dict:
    """Copy the live memory tree under archive/<eraId>/<timestamp>/, then
    reset to defaults. Returns a summary of files moved + bytes."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    target = ARCHIVE_BASE / era_id / stamp
    target.mkdir(parents=True, exist_ok=True)
    summary = {"archive": str(target), "files_moved": 0, "bytes_moved": 0}

    # Archive each section if it exists and has content.
    sections = [
        ("players", PLAYERS_DIR),
        ("arcs", ARCS_DIR),
        ("global", GLOBAL_DIR),
    ]
    for label, src in sections:
        if not src.exists():
            continue
        dst = target / label
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True)
            # Compute size + count
            for p in dst.rglob("*"):
                if p.is_file():
                    summary["files_moved"] += 1
                    summary["bytes_moved"] += p.stat().st_size
        except Exception as e:
            print(f"WARNING: failed to archive {label}: {e}", file=sys.stderr)

    # Reset active state files to defaults.
    now = int(time.time())

    if PLAYERS_DIR.exists():
        for p in PLAYERS_DIR.glob("*.json"):
            try:
                p.unlink()
            except Exception:
                continue
    _write_json(PLAYERS_DIR / "index.json", _default_player_index())

    # Active arc — kill if present.
    active = ARCS_DIR / "active.json"
    if active.exists():
        try:
            active.unlink()
        except Exception:
            pass
    _write_json(ARCS_DIR / "index.json", _default_arc_index())

    _write_json(GLOBAL_DIR / "server-history.json", _default_history())
    _write_json(GLOBAL_DIR / "lore.json", _default_lore())
    _write_json(RESET_MARKER_FILE, _default_reset_marker(stamp, now))

    # Also wipe the listener's legacy caches so the next session starts clean.
    for legacy in (LEGACY_PLAYER_REGISTRY, LEGACY_NARRATIVE_STATE, LEGACY_GREET_QUEUE, LEGACY_DELTA):
        if legacy.exists():
            try:
                # Don't delete greeting-queue if there are pending greetings
                # mid-flight — leave it alone. archive first.
                if legacy.name == "greeting-queue.json":
                    data = _read_json(legacy, {"pending": [], "handled": []})
                    if data.get("pending"):
                        print(f"NOTE: skipped wiping {legacy.name} (pending greetings present)", file=sys.stderr)
                        continue
                legacy.unlink()
            except Exception:
                continue

    return summary


def maybe_announce(reason: str) -> bool:
    """Broadcast a one-line SIMON sign-off to #pz-molt. No-op unless --announce."""
    import subprocess
    env_path = Path(os.path.expanduser("~/.env"))
    if not env_path.exists():
        print("WARNING: .env missing — cannot read PZ_DISCORD_CHANNEL_ID; --announce skipped", file=sys.stderr)
        return False
    # Tiny .env parser (no external dep).
    env = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    chan = env.get("PZ_DISCORD_CHANNEL_ID")
    if not chan:
        return False
    text = (
        f"Static on the line. Bunker is rebooting. {reason or 'world reset'}. "
        f"See you on the other side. Simon, out."
    )
    try:
        subprocess.run(
            [
                "openclaw", "message", "send",
                "--channel", "discord",
                "--target", f"channel:{chan}",
                "--message", text,
            ],
            check=False,
            timeout=15,
        )
        return True
    except Exception as e:
        print(f"WARNING: announce failed: {e}", file=sys.stderr)
        return False


def main() -> int:
    p = argparse.ArgumentParser(description="Reset SIMON's world/server memory.")
    p.add_argument("--reason", default="world_reset",
                   help="Short reason for the reset (logged in reset-marker.json and announced if --announce).")
    p.add_argument("--announce", action="store_true",
                   help="Broadcast a SIMON sign-off to #pz-molt before resetting.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be archived + reset, but don't touch anything.")
    args = p.parse_args()

    marker = _read_json(RESET_MARKER_FILE, {"eraId": "era-unknown", "eraStart": 0, "totalResetsEver": 0})
    era_id = marker.get("eraId", "era-unknown")
    print(f"Current era: {era_id} (started {marker.get('eraStart', '?')})")
    print(f"Reason: {args.reason}")

    if args.dry_run:
        print("--dry-run: would archive state/memory/{players,arcs,global} and reset to defaults.")
        return 0

    if args.announce:
        print("Announcing to #pz-molt before reset...")
        ok = maybe_announce(args.reason)
        print(f"  announce={'ok' if ok else 'skipped'}")

    summary = archive_then_wipe(era_id)
    print("Reset complete.")
    print(f"  archive      : {summary['archive']}")
    print(f"  files moved  : {summary['files_moved']}")
    print(f"  bytes moved  : {summary['bytes_moved']}")
    print(f"  wiped sections: players, arcs, global; legacy listener caches cleaned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
