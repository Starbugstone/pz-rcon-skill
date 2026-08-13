# Setup & Operations (Non-Skill Docs)

This file contains setup/runbook material that is useful for operators but not required in the active skill reference set.

## Install / Enable

1. Enable Discord on your Project Zomboid server config (`servertest.ini`):
   - `DiscordEnable=true`
   - `<DISCORD_TOKEN>=<your_bot_token>`
   - `DiscordChannelID=<pz-molt chat channel id>`
   - `DiscordCommandChannel=<pz-molt-commands channel id>`
2. Invite the PZ relay bot to both channels and record its user ID.
3. Configure env in `~/.env` (local only) using `skills/pz-director/.env.example`.
4. Load the skill into OpenClaw (`skills/pz-director/` folder or packaged `.skill`).
5. Configure the systemd user service at `~/.config/systemd/user/simon-fast-listener.service` (template in `references/`).

## Discord chat ↔ in-game relay

PZ's native Discord bridge mirrors `#pz-molt` ↔ in-game chat automatically:

- Player in-game: "...help us!" → appears in `#pz-molt` as `PlayerName: ...help us!`
- SIMON's final assistant turn → cron announce delivery → `#pz-molt` → PZ relay → in-game chat

**Important:** Never use the console channel (`#pz-molt-commands`) for player-facing chatter. It's reserved for raw server mutation commands.

## Console mutation channel

`#pz-molt-commands` is the new server-console interface. The PZ server's relay bot listens for raw console commands and forwards them to the server console. The bot then posts the server's response back into the same channel.

- **Only the PZ relay bot** is a trusted author on this channel. All other messages (humans, other bots, SIMON's own account) are ignored regardless of @mention.
- Filter on `message.author.id` (the bot's user ID), **never** on `author.username` — the bot can post under varying display names depending on server config.
- Use the wrapper script `scripts/pz-console.sh <command>` to post commands with proper quoting.

## Listener / daemon

The Discord listener (`scripts/simon_fast_listener.py`) is the runtime bridge between Discord traffic and SIMON's cron-driven ticks:

- Reacts to relay-bot messages in both channels
- Queues player connection events for the greeting dispatcher
- Caches the last relay-bot response for the ambient-trigger gate
- Filters on `PZ_RELAY_BOT_ID` to ignore noise

Run as a systemd user service for auto-restart on crash. Example unit lives in `references/` (template).

## Mods planning / onboarding

Mod planning docs are intentionally kept outside active runtime references:
- `skills/pz-director/references/installed-mods.md`

When enabling mods for runtime lookup:
- Set `PZ_ENABLED_MODS` in `~/.env`
- Add per-mod item files to:
  - `skills/pz-director/references/catalogs/mods/mod-<modname>-items.md`
