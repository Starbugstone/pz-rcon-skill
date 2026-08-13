#!/usr/bin/env bash
# pz-console.sh — Post raw PZ console commands via Discord relay.
#
# Mirrors the legacy pz-rcon.sh interface for backward compatibility, but
# routes through the PZ server's Discord chat relay instead of RCON. The PZ
# server has DiscordEnable=true with DiscordCommandChannel set; raw messages
# posted here are forwarded to the server console and responses are echoed
# back by the relay bot.
#
# Usage: pz-console.sh <command> [args...]
#
# Mapped commands (caller-friendly → raw console command):
#   players                                       → players
#   give <user> <Module.Item> [count]             → additem "<user>" <Item> <count>
#   vehicle <VehicleScript> <user>                → addvehicle "<script>" "<user>"
#   horde <count> [user]                          → createhorde <count> "<user>"
#   xp <user> <Perk>=<amount>                     → addxp "<user>" <Perk>=<amount>
#   chopper | gunshot | alarm                     → <verb>
#   lightning [user] | thunder [user]             → <verb> "<user>"
#   rain start [i] | rain stop | rain <i>         → startrain|stoprain
#   storm [hours]                                 → startstorm <hours>
#   clear | weather-stop                          → stopweather
#   msg <text> | say <text> | servermsg <text>    → <text> (admin escape hatch)
#   raw <cmd...> | cmd <cmd...>                   → <cmd...>
#   anything else                                 → passed through as-is
#
# Behavior:
#   - Posts the resolved raw command to #pz-molt-commands via OpenClaw's
#     message tool (uses PZ_DISCORD_COMMANDS_CHANNEL_ID from .env).
#   - By default, polls the channel for the relay bot's response and prints
#     it to stdout (up to PZ_CONSOLE_TIMEOUT seconds).
#   - Set PZ_CONSOLE_WAIT=0 in .env for fire-and-forget mode.
#   - On any error (no channel id, post failure): prints error to stderr,
#     exits non-zero.
#
# Configuration (in ~/.env):
#   PZ_DISCORD_COMMANDS_CHANNEL_ID  required (no default — must be set)
#   PZ_CONSOLE_TIMEOUT              optional (default: 8 seconds)
#   PZ_CONSOLE_WAIT                 optional (default: 1 = wait; 0 = skip)

set -uo pipefail

ENV_FILE="${ENV_FILE:-$HOME/.env}"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

if [ -z "${PZ_DISCORD_COMMANDS_CHANNEL_ID:-}" ]; then
    echo "ERROR: PZ_DISCORD_COMMANDS_CHANNEL_ID not set in $ENV_FILE" >&2
    exit 1
fi

if [ "$#" -eq 0 ]; then
    cat <<'USAGE' >&2
Usage: pz-console.sh <command> [args...]

Common commands:
  players                                       list online players
  give <user> <Module.Item> [count]             additem
  vehicle <VehicleScript> <user>                addvehicle
  horde <count> [user]                          createhorde
  xp <user> <Perk>=<amount>                     addxp
  chopper | gunshot | alarm                     sound events
  lightning [user] | thunder [user]             atmospheric events
  rain start [intensity] | rain stop | rain <i> precipitation
  storm [hours]                                 startstorm
  clear | weather-stop                          stopweather
  msg <text> | say <text> | servermsg <text>    admin escape hatch
  raw <cmd...> | cmd <cmd...>                   passthrough
USAGE
    exit 1
fi

# Resolve caller-friendly command to raw console command.
cmd="$1"
shift

raw=""
case "$cmd" in
    players|list)
        raw="players"
        ;;
    give)
        [ "$#" -ge 2 ] || { echo "ERROR: give requires <user> <item> [count]" >&2; exit 1; }
        user="$1"; item="$2"; count="${3:-1}"
        # Quote both user and item: relay bot requires "username" "module.item" count.
        # Without quotes, an item id containing a dot (e.g. Base.Bandage) plus a
        # numeric count can still parse, but a duplicated item id like
        # `additem "X" Base.Bandage Base.Bandage` is rejected (count is non-numeric).
        raw="additem \"${user}\" \"${item}\" ${count}"
        ;;
    vehicle)
        [ "$#" -ge 2 ] || { echo "ERROR: vehicle requires <script> <user>" >&2; exit 1; }
        raw="addvehicle \"$1\" \"$2\""
        ;;
    horde)
        [ "$#" -ge 1 ] || { echo "ERROR: horde requires <count> [user]" >&2; exit 1; }
        if [ "$#" -ge 2 ]; then
            raw="createhorde $1 \"$2\""
        else
            raw="createhorde $1"
        fi
        ;;
    xp)
        [ "$#" -ge 2 ] || { echo "ERROR: xp requires <user> <Perk>=<amount>" >&2; exit 1; }
        raw="addxp \"$1\" $2"
        ;;
    chopper|gunshot|alarm)
        raw="$cmd"
        ;;
    lightning|thunder)
        if [ "$#" -ge 1 ]; then
            raw="$cmd \"$1\""
        else
            raw="$cmd"
        fi
        ;;
    rain)
        [ "$#" -ge 1 ] || { echo "ERROR: rain requires start|stop|<intensity>" >&2; exit 1; }
        case "$1" in
            start) [ "$#" -ge 2 ] && raw="startrain $2" || raw="startrain" ;;
            stop)  raw="stoprain" ;;
            *)     raw="startrain $1" ;;
        esac
        ;;
    storm)
        [ "$#" -ge 1 ] && raw="startstorm $1" || raw="startstorm"
        ;;
    clear|weather-stop)
        raw="stopweather"
        ;;
    msg|say|servermsg)
        raw="$*"
        ;;
    raw|cmd)
        raw="$*"
        ;;
    *)
        # Unknown command: passthrough as-is so advanced callers aren't boxed in.
        raw="$cmd $*"
        ;;
esac

# Post the raw command to Discord.
POST_OUT=$(openclaw message send \
    --channel discord \
    --target "channel:${PZ_DISCORD_COMMANDS_CHANNEL_ID}" \
    --message "$raw" 2>&1)
POST_RC=$?

if [ $POST_RC -ne 0 ]; then
    echo "ERROR: Failed to post to Discord (exit $POST_RC): $POST_OUT" >&2
    exit $POST_RC
fi

# Fire-and-forget mode.
WAIT="${PZ_CONSOLE_WAIT:-1}"
TIMEOUT="${PZ_CONSOLE_TIMEOUT:-8}"

if [ "$WAIT" = "0" ]; then
    echo "[posted: $raw]"
    exit 0
fi

# Poll the channel for the relay bot's response.
START_TS=$(date +%s)
SLEEP_INTERVAL=1

while true; do
    NOW=$(date +%s)
    ELAPSED=$((NOW - START_TS))
    if [ $ELAPSED -ge $TIMEOUT ]; then
        echo "[posted: $raw — no response after ${TIMEOUT}s]"
        exit 0
    fi

    MSGS=$(openclaw message read \
        --channel discord \
        --target "channel:${PZ_DISCORD_COMMANDS_CHANNEL_ID}" \
        --limit 3 2>/dev/null) || {
        sleep $SLEEP_INTERVAL
        continue
    }

    RESPONSE=$(echo "$MSGS" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    msgs = data.get("payload", {}).get("messages", data.get("messages", []))
    if not msgs:
        sys.exit(0)
    for m in msgs:
        author = m.get("author", {})
        if author.get("bot"):
            print(m.get("content", ""))
            sys.exit(0)
except Exception:
    pass
')

    if [ -n "$RESPONSE" ]; then
        echo "$RESPONSE"
        exit 0
    fi

    sleep $SLEEP_INTERVAL
done
