# pz-director

SIMON's controller skill for the Project Zomboid server. Discord-first architecture; no RCON.

## TL;DR

- Two Discord channels, two paths: `#pz-molt` for chat, `#pz-molt-commands` for server mutations.
- Post mutations with `./scripts/pz-console.sh <command>`. It forwards to the PZ server console via Discord.
- Listener is `scripts/simon_fast_listener.py`. It monitors both channels with strict author filtering on the commands channel.
- See `SKILL.md` for the full architecture doc and security rules.

## Deploy

1. Make sure `.env` has `PZ_DISCORD_CHANNEL_ID`, `PZ_DISCORD_COMMANDS_CHANNEL_ID`, and `PZ_RELAY_BOT_ID` set. No defaults.
2. Start the listener: `python3 scripts/simon_fast_listener.py` (or run it under `simon_fast_listener` systemd unit if you've created one).
3. The SIMON Ambient Director cron job drives periodic broadcasts. It's configured in OpenClaw's `cron_jobs` table; payload points at `scripts/simon_ambient_trigger.py` for the trigger gate and the LLM-driven body.

## Troubleshooting

- Listener exits with "ERROR: PZ_* missing from .env" → set the required variable.
- Commands return "Unknown command" → the PZ server may not have `DiscordCommandChannel` set; check the server config on the host you admin.
- `pz-console.sh` times out → bump `PZ_CONSOLE_TIMEOUT` in `.env`, or check that the relay bot is actually responding in `#pz-molt-commands`.
- Listener doesn't pick up chat relay messages → verify the relay bot ID matches `PZ_RELAY_BOT_ID`. The bot username can change; the ID is stable.

## Files

- `SKILL.md` — full architecture / security doc.
- `scripts/pz-console.sh` — post raw console commands.
- `scripts/simon_fast_listener.py` — Discord listener (chat + commands).
- `scripts/simon_ambient_trigger.py` — cron trigger gate (10-min Ambient Director).
- `scripts/greeting_trigger.py` — cron trigger gate (1-min Greeting Dispatcher).
- `references/` — mod catalogs, operator docs.