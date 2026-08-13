---
name: pz-director
description: SIMON's Project Zomboid server control skill. Discord-first architecture — chat broadcasts and server-state mutations both routed through Discord. NO RCON. Use this skill for anything related to the PZ server SIMON is connected to.
---

# pz-director — SIMON's Project Zomboid Controller (Discord-first)

SIMON is the bunker-radio operator for a Project Zomboid apocalypse server. This skill is how SIMON talks to the world and how SIMON mutates the server. **The architecture is 100% Discord-based.** RCON is deprecated and the RCON port/credentials are intentionally disabled on this controller host.

## Architecture (read this first)

The PZ server itself has `DiscordEnable=true` with two Discord channels wired:

- **Chat channel** (`DiscordChatChannel=pz-molt`, ID stored in `.env` as `PZ_DISCORD_CHANNEL_ID`) — PZ mirrors in-game chat, system events, and player connections into this Discord channel. Messages SIMON posts here are mirrored back into in-game chat. **This is the broadcast / chat path.**
- **Commands channel** (`DiscordCommandChannel=pz-molt-commands`, ID stored in `.env` as `PZ_DISCORD_COMMANDS_CHANNEL_ID`) — Raw console commands SIMON posts here are forwarded to the PZ server console; the relay bot posts the server's response back into the same channel. **This is the mutation / state-change path.**

SIMON uses **two distinct channels** because they have **two distinct trust models**:

| Path | Channel | Author filter | Trust |
|------|---------|---------------|-------|
| Chat / broadcast | `#pz-molt` | PZ relay bot allowed; other bots filtered | Players, in-game chat mirror, connection events |
| Server mutation | `#pz-molt-commands` | **PZ relay bot only — humans and other bots ignored regardless of @mention** | Only the relay bot's authoritative server responses |

**Why two channels, bot-only filter on commands?** The commands channel is the new game-server console interface. Letting any human or arbitrary bot post there would let any channel member social-engineer SIMON into echoing arbitrary text back into the game server. The relay bot is the only trusted originator. Always filter on `message.author.id` (from `.env` `PZ_RELAY_BOT_ID`), **never on `author.username`** — the relay bot can post under varying usernames (`servertest`, `pz-server`, `PZ-Molt-Bot`) depending on server config.

## What lives where

```
pz-director/
├── SKILL.md                      this file
├── README.md                     operator notes (deployment, troubleshooting)
├── .env.example                  template for required .env variables
├── config.json.example           template for runtime config
├── scripts/
│   ├── pz-console.sh             posts raw PZ console commands to #pz-molt-commands
│   ├── simon_fast_listener.py    Discord bot: chat channel + commands channel (bot-only)
│   ├── simon_ambient_trigger.py  cron trigger gate (10-min, reads listener cache)
│   └── greeting_trigger.py       cron trigger gate (1-min, reads greeting queue)
├── state/                        runtime state (gitignored; archive/ holds historical)
└── references/                   static operator docs, mod catalogs
```

## Configuration

All real values live in `~/.env` (gitignored). Required:

- `PZ_DISCORD_CHANNEL_ID` — `#pz-molt` chat channel ID (no default)
- `PZ_DISCORD_COMMANDS_CHANNEL_ID` — `#pz-molt-commands` ID (no default)
- `PZ_RELAY_BOT_ID` — PZ server's relay bot user ID (no default; filter on ID, not name)
- `PZ_ENABLED_MODS` — comma-separated mod list

Optional:

- `PZ_CONSOLE_WAIT` — `1` (default) waits for relay bot response; `0` for fire-and-forget
- `PZ_CONSOLE_TIMEOUT` — seconds to wait before giving up (default 8)

**No default values for the IDs.** If a required ID is missing, scripts fail loud at startup.

## Usage

### Run the listener

```bash
python3 scripts/simon_fast_listener.py
```

It opens a Discord connection and monitors both channels with the rules above.

### Post a server mutation

```bash
./scripts/pz-console.sh players
./scripts/pz-console.sh give Stone Base.WaterBottle 1
./scripts/pz-console.sh addvehicle "Base.86oshkosh" "Stone"
./scripts/pz-console.sh startstorm 4
```

`pz-console.sh` posts the raw console command to `#pz-molt-commands` and (by default) polls for the relay bot's response and prints it. Set `PZ_CONSOLE_WAIT=0` in `.env` for fire-and-forget.

### Broadcast into in-game chat

```bash
# From inside an agent turn, output the broadcast text as your final reply.
# The cron delivery layer announces it to #pz-molt, and the PZ chat relay
# mirrors it into in-game chat. NO pz-console.sh call needed for chat.
```

### Check who's online

```bash
./scripts/pz-console.sh players
```

## Author-filter rule (security-critical)

When the listener encounters a message in `#pz-molt-commands`, it applies this filter:

```python
if message.author.bot:
    if message.author.id == PZ_RELAY_BOT_ID:   # relay bot — authoritative server response
        process()
    else:
        ignore()                              # other bots — noise
else:
    ignore()                                  # humans — never process, even on @mention
```

**No exceptions, no @mention bypass.** The commands channel is a console interface, not a chat. If a human wants SIMON to do something, they talk to SIMON in `#pz-molt` or DM.

## Disabled / removed

- **RCON path is gone.** `.env` has `PZ_RCON_HOST=disabled`, `PZ_RCON_PORT=0`. The SIMON Ambient Director cron job is disabled (`enabled=0` in `cron_jobs`). Re-enable only if Stone decides the Discord relay is insufficient for some operation and explicitly asks.
- **FTP log reader (`event_monitor.py`) is gone.** The new server has no FTP path from this controller host; mod-event detection was a screenlog reader, now dead.
- **`pz-rcon.sh`, `connection_listener.sh`, `set_player_{honorific,nickname}.py`, `simon_fast_listener.service`** — deleted. RCON-only paths with no Discord equivalent.
- **`references/archive/PZ-RCON-GUIDE.md`, `references/commands.md`, `pz-rcon.skill` bundle** — deleted.

## Cross-agent notes

This skill lives at `~/.openclaw/workspace-simon/skills/pz-director/`. A symlink may exist at `~/.openclaw/workspace/skills/pz-rcon` for backwards compatibility (legacy name kept to avoid breaking references that may still point to it); treat the canonical path as `pz-director`.