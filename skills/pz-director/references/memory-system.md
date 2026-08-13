# SIMON Memory System

Operator reference for SIMON's three-layer memory architecture. Read this
once before touching memory code; the helper modules are designed so you
almost never need to read/write these files directly.

## Why three layers

SIMON has three memory tiers with very different lifetimes:

1. **Per-player memory** — durable across arcs and sessions. SIMON knows
   each survivor, references past chats, calls back to their quirks, and
   remembers which arc they lived through.
2. **Per-arc memory** — temporary. Lives only while an arc is unraveling
   (a few hours). Captures beat-by-beat narration, mutations executed,
   and live player interactions. Auto-deleted when the arc terminates
   or when no players are online for >30 minutes.
3. **Global server memory** — durable across arcs. The running "state of
   the world." SIMON reads this when greeting a new or returning player
   to set the bunker radio's continuity.

The three layers never duplicate a thing. Each layer has one job:

```
                ┌─────────────────────────────────┐
                │ Global server memory            │
                │ state/memory/global/            │
                │ - server-history.json (events)  │
                │ - lore.json (synthesized)       │
                │ - reset-marker.json (eras)      │
                └─────────────────────────────────┘
                                ▲
                                │ summarized from
                                │
                ┌─────────────────────────────────┐
                │ Active arc memory (in-flight)   │◀── short-lived
                │ state/memory/arcs/active.json   │    (4h cooldown,
                │ + final-state on completion     │    auto-deleted
                │   archived to arcs/archive/     │    after 30m no
                │ + summary to arcs/index.json    │    players online)
                └─────────────────────────────────┘
                                ▲ feeds
                                │
                ┌─────────────────────────────────┐
                │ Per-player profiles (durable)   │◀── append-only
                │ state/memory/players/<slug>.json│    ring buffers
                │ + players/index.json pointer   │    cap each player
                │ + legacy player-registry.json   │    profile fields
                └─────────────────────────────────┘
```

## Layer 1: per-player memory (`state/memory/players/`)

**Writes:**
- `simon_player_memory.bump_visit(name)` — called by the listener on
  every connection event for a player.
- `simon_player_memory.record_interaction(name, kind, content, trigger?,
  simon_reply_summary?)` — called by the listener on every chat parsed
  from the PZ chat relay.
- `simon_player_memory.append_note(name, text)` — LLM may write a free-form
  fact about a player (e.g. "asks about storms often", "favors Louisville").
- `simon_player_memory.append_arc_recap(name, arc_id, summary)` — called
  when an arc finalizes, capturing the player's experience during it.
  Also called by the ambient cron payload after a beat fires when the
  player was online.

**Reads:**
- `simon_player_memory.get_brief(name)` — formatted paragraph for the LLM
  prompt so SIMON can reference tier, past chats, notes, and last arc in
  chat/greeting/ambient responses.

**Caps (in module):**
- `recentChats` ring: last 20 entries
- `arcRecaps` ring: last 8 entries
- `notes`: last 12 entries
- `callbacksSeeded`: last 40 (dedupe helper)

**Slug rule:** player names get lowercased + spaces-to-dashes. `Big Mike`
becomes `big-mike`. Files go in `state/memory/players/<slug>.json`. The
display `name` field keeps the original casing.

**Legacy mirror:** `bump_visit` and friends also write a minimal entry to
`state/player-registry.json` so the listener's existing tier detection
keeps working with no edits to that path. New code should ignore
`player-registry.json` and use `state/memory/players/<slug>.json` directly.

## Layer 2: per-arc memory (`state/memory/arcs/`)

**Lifecycle:**
1. `simon_arc_engine.start_arc()` — picks a catalog arc, creates
   `active.json`. Refuses if an arc is already active OR if the last arc
   finished within `ARC_RESET_HOURS = 4` hours.
2. While active, every cron tick the ambient payload calls
   `simon_arc_engine.advance_beat()`. Returns `{arcId, beatIdx, narration,
   mutation}` when `now - lastBeatTs >= beats[idx].gapMin * 60`.
3. After broadcasting, the payload calls
   `simon_arc_engine.mark_beat_fired(payload, simon_narration)` to log the
   narration and bump `lastBeatTs`. When `currentBeatIdx >= len(beats)`,
   the engine auto-finalizes as `reason='completed'`.
4. Listener calls `record_player_join/leave/interaction` so SIMON's
   in-character chat replies during the arc have context.
5. Every tick the payload calls `cleanup_stale_arc()`. If
   `playersOnline == []` and `now - lastPlayerLeftTs > 30 min`, the arc
   is finalized as `reason='abandoned_no_players_30m'`.

**Active state file (`active.json`) holds:**
- `arcId, arcName, summary` (from the catalog)
- `arcStartedTs, currentBeatIdx, lastBeatTs`
- `beats[]` (copied from the catalog)
- `beatHistory[]` (per-beat record: ts, planned/actual narration, mutation)
- `playersOnline[]`, `lastPlayerLeftTs`
- `narrations[]` (last 50 actual narration strings, for lore rebuilding)
- `playerInteractions[]` (last 60 entries: ts, player, content, reply)

**On finalize:**
1. Append 1-line summary to `arcs/index.json` (capped at last 50).
2. Copy full `active.json` to `arcs/archive/<arcId>-<timestamp>.json`.
3. Log event to `global/server-history.json`.
4. For each player who was `playersInvolved`, call
   `simon_player_memory.append_arc_recap()`.
5. Delete `active.json`.

## Layer 3: global server memory (`state/memory/global/`)

**Files:**
- `server-history.json` — chronological event log. Cap: 200 most-recent
  events. Older ones rotate into `archive/history-<timestamp>.json` when
  `trim_history()` runs.
- `lore.json` — running "state of the world" narrative. Rebuilt by
  `rebuild_lore_from_history()` whenever an arc finalizes.
- `reset-marker.json` — tracks the era ID, eraStart timestamp, and total
  resets.

**Writes:**
- `simon_global_memory.log_event(event)` — called by the arc engine
  (`finalize_arc`) and optionally by the ambient payload for major
  non-arc broadcasts.
- `simon_global_memory.append_lore(narrative=..., fact_merge=...)` —
  replace the lore narrative or merge facts.

**Reads (used in greetings):**
- `build_returning_player_brief()` — single-paragraph lore line for
  tier=returning/veteran players.
- `build_new_player_brief()` — lore line + welcome blurb for tier=new.

## Cleanup triggers (summary)

| Trigger | What | Where |
|---|---|---|
| Arc finalizes | Delete active.json, archive full state, summarize to index.json | `simon_arc_engine.finalize_arc` |
| No players online for 30+ min during an active arc | Same as finalize, but reason='abandoned_no_players_30m' | `simon_arc_engine.cleanup_stale_arc` |
| Server history exceeds 200 entries | Rotate older events into `archive/history-*.json` | `simon_global_memory.trim_history` |
| Operator resets the world | Archive full memory tree, wipe to defaults, bump reset-marker | `simon_reset_world.py` |
| 4 hours between arcs | Cooldown window via `ARC_RESET_HOURS` | `simon_arc_engine.start_arc` |

## Reset script — operator usage

```bash
# Preview what would happen (no side effects)
python3 scripts/simon_reset_world.py --dry-run

# Standard reset, silent (no broadcast)
python3 scripts/simon_reset_world.py --reason "world v2 fresh start"

# Reset + announce to #pz-molt (SIMON signs off the old era on the radio)
python3 scripts/simon_reset_world.py --reason "world v2 fresh start" --announce
```

After a reset:
- `state/memory/players/` is empty (only `index.json` remains).
- `state/memory/arcs/active.json` is gone; `index.json` is empty.
- `state/memory/global/server-history.json` is empty.
- `state/memory/global/lore.json` is back to the default template.
- `state/memory/global/reset-marker.json` bumps `totalResetsEver` and
  stamps a new `eraId` ("era-<timestamp>").
- The previous era's full state is preserved at
  `state/memory/archive/<old-era-id>/<timestamp>/{players,arcs,global}/`.

## What still lives where (not memory)

Listener ephemeral state — separate from the memory system:
- `state/player-registry.json` — legacy mirror, written by `bump_visit`.
- `state/player-delta.json` — online players this session.
- `state/narrative-state.json` — broadcast cap bookkeeping.
- `state/greeting-queue.json` — pending greeting requests.
- `state/discord-message-state.json` — last-seen message id.

These are wiped by `simon_reset_world.py` only when safe (e.g. the
greeting-queue.json is preserved if there are pending greetings).
