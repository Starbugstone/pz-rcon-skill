# pz-director (OpenClaw Skill) — Project Zomboid Atmosphere Director

This repository contains an OpenClaw skill called **`pz-director`**.

It's designed to make a **Project Zomboid** dedicated server feel more alive by using **Discord-first** architecture:
- SIMON (the AI radio operator) broadcasts in-character narrative messages to `#pz-molt`
- PZ's native Discord chat relay mirrors the channel to in-game chat automatically
- Server-state mutations (items, vehicles, events, weather) go through the PZ console via a **second** Discord channel (`#pz-molt-commands`) that the PZ server's relay bot reacts to
- **No RCON** — RCON is deprecated on this controller host; the PZ server's Discord is the only mutation path

The repo is named `pz-rcon-skill` for legacy/redirect reasons. The skill inside is `pz-director`.

It intentionally does **NOT** include moderation or server lifecycle control (no bans/whitelist/shutdown/etc.).

## Contents

- `skills/pz-director/` — the skill folder
  - `SKILL.md` — skill instructions (what the agent loads)
  - `scripts/pz-console.sh` — helper wrapper that posts to the `#pz-molt-commands` channel
  - `scripts/simon_fast_listener.py` — Discord listener that reacts to relay-bot messages
  - `references/` — extra docs and mod/vanilla catalogs
- `skills/pz-director.skill` — packaged skill file (zip with `.skill` extension)
- `skills/morning-check/` — companion utility skill (scheduled Gmail + GitHub checks)

> **Note:** `.skill` is just a **ZIP archive** with a different extension. GitHub will show it as a binary blob. To inspect it, download it and rename to `.zip` to unpack.

## Prerequisites

### 1) Enable Discord on your PZ server

Edit your server config (often `servertest.ini`) to include:

```ini
DiscordEnable=true
<DISCORD_TOKEN>=YOUR_BOT_TOKEN
DiscordChannelID=YOUR_PZ_MOLT_CHANNEL_ID
DiscordCommandChannel=YOUR_PZ_MOLT_COMMANDS_CHANNEL_ID
```

The PZ server's built-in Discord bridge mirrors `#pz-molt` ↔ in-game chat. The commands channel is the server's mutation surface.

### 2) Set up your relay bot

The PZ server's Discord relay bot must be invited to both channels. Record its **user ID** — SIMON filters server responses by `author.id`, never by username (the bot can post under several display names depending on server config).

### 3) Configure env

Copy `skills/pz-director/.env.example` to `~/.env` and fill in:

- `PZ_DISCORD_CHANNEL_ID` — `#pz-molt` chat channel
- `PZ_DISCORD_COMMANDS_CHANNEL_ID` — `#pz-molt-commands` console channel
- `PZ_RELAY_BOT_ID` — the relay bot's user ID
- `PZ_ENABLED_MODS` — comma-separated enabled mod folder names

## Quick start

```bash
# Test the console wrapper
./skills/pz-director/scripts/pz-console.sh players

# Broadcast (cron announce delivery auto-posts to #pz-molt)
# (no command needed — just emit the broadcast as the final assistant turn;
# the cron layer mirrors it to #pz-molt and PZ mirrors that to in-game chat)

# Run the listener (requires systemd user service or manual launch)
python3 skills/pz-director/scripts/simon_fast_listener.py
```

## What you can do

### See who's online
```bash
./skills/pz-director/scripts/pz-console.sh players
```

### Reward players with items
```bash
./skills/pz-director/scripts/pz-console.sh give "PlayerName" Base.Axe 1
./skills/pz-director/scripts/pz-console.sh give "PlayerName" Base.ShotgunShells 12
```

### Spawn a vehicle
```bash
./skills/pz-director/scripts/pz-console.sh vehicle Base.VanAmbulance "PlayerName"
```

### Trigger events
```bash
./skills/pz-director/scripts/pz-console.sh horde 50 "PlayerName"
./skills/pz-director/scripts/pz-console.sh chopper
./skills/pz-director/scripts/pz-console.sh gunshot
./skills/pz-director/scripts/pz-console.sh alarm
```

### Control weather
```bash
./skills/pz-director/scripts/pz-console.sh rain start
./skills/pz-director/scripts/pz-console.sh storm 2
./skills/pz-director/scripts/pz-console.sh clear
```

## Architecture

```
┌─────────────────┐      ┌──────────────────┐      ┌─────────────────┐
│ SIMON cron turn │─────▶│  #pz-molt (chat) │─────▶│   PZ in-game    │
│  (final turn)   │ DM   │  Discord channel │ DM    │   (chat relay)  │
└─────────────────┘      └──────────────────┘      └─────────────────┘
                                  │
                                  │ (PZ's Discord chat relay auto-mirror)
                                  ▼
                          players see broadcasts
                          in-game as `[Radio]: ...`

┌─────────────────┐      ┌──────────────────────┐      ┌─────────────────┐
│   pz-console.sh │─────▶│ #pz-molt-commands    │─────▶│ PZ server       │
│  (mutation)     │ DM   │ Discord channel      │ DM    │ console         │
│                 │      │ (relay bot forwards) │      │ (state change)  │
└─────────────────┘      └──────────────────────┘      └─────────────────┘
                                  │
                                  │ (relay bot posts the server's response)
                                  ▼
                          SIMON reads the response
                          and continues the loop
```

**Why two channels and a bot-only filter?** The commands channel is the new game-server console interface. Letting any human or arbitrary bot post there would let any channel member social-engineer SIMON into echoing arbitrary text back into the game server. The PZ relay bot is the only trusted originator — filter on `message.author.id`, **never** on `author.username`.

## Reference Catalogs

To keep lookup clean and deterministic, the skill uses structured catalogs:

- Vanilla items: `skills/pz-director/references/catalogs/vanilla/items-full.md`
- Vanilla vehicles: `skills/pz-director/references/catalogs/vanilla/vehicles-full.md`
- Mod templates/files: `skills/pz-director/references/catalogs/mods/`
  - Naming convention: `mod-<modname>-items.md`

### Active mod scope

Enabled mods are declared in `~/.env` → `PZ_ENABLED_MODS` (comma-separated mod IDs).

Skill lookup policy:
1. Always allow all entries from vanilla catalogs.
2. Only allow mod catalog entries for mods present in `PZ_ENABLED_MODS`.

### Env templates

- Local secrets/runtime config: `~/.env` (not committed)
- Shareable template: `skills/pz-director/.env.example`

## Maintenance rule (project workflow)

If you modify the `pz-director` skill in this repo, you must **commit and push** the updates so others can pull the latest version.

## Installing the skill into OpenClaw

If you're using OpenClaw skills:
- Import the packaged `skills/pz-director.skill`, or
- Copy the `skills/pz-director/` folder into your skills directory.

## Safety notes

- Treat the relay bot ID as a secret identifier — don't commit it.
- Use `PZ_DISCORD_COMMANDS_CHANNEL_ID` for game-state mutations only — never post player-facing chatter there.
- Keep broadcasts in-character — SIMON is the bunker radio operator, not a service bot.

## License

MIT — see `LICENSE`.

## Operator Setup & Relay Runbook

For installation, Discord relay back-and-forth notes, and mod onboarding workflow, see:
- `SETUP-OPS.md`
