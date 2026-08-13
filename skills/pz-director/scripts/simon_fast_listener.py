#!/usr/bin/env python3
"""
SIMON Fast Listener — Discord-based player connection detector + chat responder.
Runs as a background service, detects "[PlayerName] connected to server"
messages in #pz-molt and fires Discord greetings within seconds. Also listens
to general in-game chat (mirrored to #pz-molt via PZ's Discord chat relay) and
responds in-character when addressed.

Architecture: PZ server has DiscordEnable=true and DiscordChatChannel=pz-molt,
so messages sent to #pz-molt are automatically mirrored to in-game chat via
PZ's Discord chat relay. Mutations are posted to #pz-molt-commands via
pz-console.sh (the PZ relay bot echoes server responses back into that channel).

Two channels, two rules:
1. #pz-molt (PZ_DISCORD_CHANNEL_ID) — chat/broadcast path. Connection events,
   in-game chat mirror, player-direct mentions. Standard trigger gating.
2. #pz-molt-commands (PZ_DISCORD_COMMANDS_CHANNEL_ID, optional) — bot-only
   channel. SIMON only reacts to messages from the PZ relay bot
   (PZ_RELAY_BOT_ID). Humans and other bots are ignored regardless of @mention.

Usage:
    python3 simon_fast_listener.py

Requires:
    - discord.py (pip install discord.py)
    - Bot token from OpenClaw config (read from openclaw.json)
    - ~/.env with PZ_RELAY_BOT_ID and PZ_DISCORD_CHANNEL_ID
"""

import asyncio
import json
import os
import sys
import subprocess
import time
from pathlib import Path

# Configuration
SKILL_DIR = Path(__file__).parent.parent
STATE_DIR = SKILL_DIR / "state"

# Memory subsystem (per-player profiles + arc engine).
# Imports live here so the listener and the helpers share the same Python
# process and the same filesystem view. Failures are non-fatal — when a
# helper can't be imported, the listener falls back to the legacy in-memory
# tier detection so we never lose player-side traffic just because the
# memory system is offline.
try:
    import simon_player_memory  # state/memory/players/<slug>.json
    import simon_arc_engine      # state/memory/arcs/{active,index}.json
except Exception as _mem_import_err:
    print(
        f"WARNING: memory helpers unavailable ({_mem_import_err}); "
        f"per-player + arc tracking disabled this run",
        file=sys.stderr,
    )
    simon_player_memory = None
    simon_arc_engine = None
GREET_DEDUPE_FILE = STATE_DIR / "last_greet.txt"
PLAYER_DELTA_FILE = STATE_DIR / "player-delta.json"
DISCORD_MESSAGE_STATE_FILE = STATE_DIR / "discord-message-state.json"
CONSOLE_SCRIPT = Path(__file__).parent / "pz-console.sh"


def load_env(path: str) -> dict:
    """Tiny .env loader — returns dict of KEY=VALUE entries (strips quotes)."""
    env = {}
    if not os.path.exists(path):
        return env
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip().strip('"').strip("'")
    except Exception as e:
        print(f"WARNING: could not read {path}: {e}", file=sys.stderr)
    return env


# Load required identity from .env. NO defaults — operator must configure.
ENV = load_env(os.path.expanduser("~/.env"))
PZ_CHAT_RELAY_BOT_ID = int(ENV["PZ_RELAY_BOT_ID"]) if ENV.get("PZ_RELAY_BOT_ID") else None

# Greetings are now LLM-generated via the greeting-queue + 1-min cron pipeline.
# See scripts/greeting_trigger.py and the "SIMON Greeting Dispatcher" cron job.
# The listener queues connection events; SIMON reads player memory and generates
# personalized greetings. No hardcoded templates.

# Dedupe window in seconds (5 minutes)
DEDUPE_SECONDS = 300

# Chat response: per-author cooldown in seconds (avoid spam)
CHAT_COOLDOWN_SECONDS = 30

# Chat response: minimum message length to consider
CHAT_MIN_LENGTH = 3

# Chat response: trigger patterns (lowercased substrings). If any of these
# appear in a message, SIMON will respond. Keep the list tight — too liberal
# makes the bot spammy.
CHAT_TRIGGER_PATTERNS = (
    # Direct address
    "simon",
    "@simon",
    "hey simon",
    "yo simon",
    "ok simon",
    # Radio lingo commonly used in PZ
    "over",
    "copy that",
    "10-4",
    "request",
    "status report",
    "anyone ",
    "anybody ",
    "help ",
    "sos",
    # Questions
    "?",
)

# CHAT_RESPONSES removed — chat responses are now LLM-generated via
# build_simons_chat_response(). The listener calls zai/glm-5.2 directly with
# a SIMON voice prompt + player memory context, so replies are personalized
# to the player instead of picked from a canned dict.

# Load bot token from .env first, falling back to openclaw.json.
# openclaw.json templates the token as "${DISCORD_BOT_TOKEN}" — the OpenClaw
# runtime expands that for itself, but raw Python scripts see the literal
# placeholder and Discord returns 401 "Improper token". Always load the real
# secret from ~/.env (gitignored, never committed). The
# openclaw.json path is kept as a fallback for any future operator who
# chooses to inline the token directly.
def load_bot_token():
    env_path = os.path.expanduser("~/.env")
    config_path = Path.home() / ".openclaw" / "openclaw.json"

    # 1) .env first (operator-only, real secret).
    env = load_env(env_path)
    token = env.get("DISCORD_BOT_TOKEN", "")

    # 2) Fall back to openclaw.json (may be templated; expand ${VAR}).
    if not token and config_path.exists():
        try:
            import re
            raw = config_path.read_text()
            def expand(match):
                return env.get(match.group(1), match.group(0))
            raw = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", expand, raw)
            cfg = json.loads(raw)
            token = cfg.get("channels", {}).get("discord", {}).get("token", "")
        except Exception as e:
            print(f"WARNING: failed to load openclaw.json: {e}", file=sys.stderr)

    if not token:
        print(
            "ERROR: Discord token not found in .env (DISCORD_BOT_TOKEN) "
            "or openclaw.json (channels.discord.token)",
            file=sys.stderr,
        )
        sys.exit(1)

    return token

# Load player registry for tier detection
def load_player_registry():
    registry_file = STATE_DIR / "player-registry.json"
    if not registry_file.exists():
        return {"players": {}}
    try:
        with open(registry_file) as f:
            return json.load(f)
    except:
        return {"players": {}}

# Get player tier based on visit count
def get_player_tier(player_name):
    registry = load_player_registry()
    player_info = registry.get("players", {}).get(player_name, {})
    visit_count = player_info.get("visitCount", 0)
    
    if visit_count <= 1:
        return "new"
    elif visit_count <= 5:
        return "returning"
    else:
        return "veteran"

# generate_greeting() removed — greetings now flow through LLM pipeline.
# fire_greeting() queues to state/greeting-queue.json; the 1-min greeting cron
# fires an agentTurn; SIMON reads player memory and generates a personalized greeting.

# Check dedupe
def should_greet(player_name):
    if not GREET_DEDUPE_FILE.exists():
        return True
    
    try:
        content = GREET_DEDUPE_FILE.read_text().strip()
        if not content:
            return True
        
        last_player, last_ts = content.split("|")
        last_ts = int(last_ts)
        now = int(time.time())
        
        # Different player or enough time passed
        if last_player != player_name or (now - last_ts) >= DEDUPE_SECONDS:
            return True
        
        return False
    except:
        return True

# Update greet dedupe file
def update_greet_dedupe(player_name):
    now = int(time.time())
    GREET_DEDUPE_FILE.write_text(f"{player_name}|{now}")

# Fire Discord greeting (Discord-first chat architecture)
# PZ server has DiscordEnable=true and DiscordChatChannel=pz-molt,
# so #pz-molt messages are mirrored to in-game chat automatically.
# RCON servermsg is no longer needed for broadcasts and is reserved
# for game-state mutations only (give, addvehicle, etc.).
async def fire_greeting(channel, player_name, max_retries=2):
    """
    Queue a greeting request for SIMON's LLM pipeline.
    
    Writes an entry to state/greeting-queue.json. The SIMON Greeting Dispatcher
    cron (1-min interval) picks it up via greeting_trigger.py, fires an isolated
    agentTurn, and SIMON generates a personalized greeting using player memory
    and interaction history. The cron announce delivers it to #pz-molt, which
    PZ's chat relay mirrors to in-game chat.
    
    No longer posts directly to Discord. All greetings are LLM-generated.
    The channel parameter is kept for signature compatibility but unused.
    
    Returns True if queued successfully.
    """
    queue_file = STATE_DIR / "greeting-queue.json"
    
    try:
        if queue_file.exists():
            queue = json.loads(queue_file.read_text())
        else:
            queue = {"pending": [], "handled": []}
    except (json.JSONDecodeError, OSError):
        queue = {"pending": [], "handled": []}
    
    entry = {
        "player": player_name,
        "ts": int(time.time()),
    }
    queue.setdefault("pending", []).append(entry)
    
    try:
        queue_file.write_text(json.dumps(queue, indent=2))
        update_greet_dedupe(player_name)
        print(f"Greeting queued for {player_name} (SIMON LLM will handle within ~1 min)")
        return True
    except Exception as e:
        print(f"ERROR: Failed to queue greeting for {player_name}: {e}", file=sys.stderr)
        return False

# Parse connection message
def parse_connection_message(content):
    """
    Parse "[PlayerName] connected to server" message.
    Returns player name or None if not a connection message.
    """
    import re
    
    # Pattern: [PlayerName] connected to server
    match = re.match(r'^\[([^\]]+)\]\s+connected to server$', content.strip())
    if match:
        return match.group(1)
    
    return None

# Parse player name from PZ Discord chat relay content.
# Format: "PlayerName: message text" — e.g. "PlayerName: simon you there?"
# Returns (player_name, message_text) or (None, original_content) if no prefix.
def parse_pz_chat_player(content):
    """
    Extract player name from PZ chat relay content.
    
    The PZ Discord chat relay posts as bot 'servertest#9150' but prepends the
    actual player's name to the message: 'PlayerName: hey simon'. We need
    the real player name (not the bot's display name) for chat responses.
    """
    import re
    
    if not content or not isinstance(content, str):
        return None, content
    
    # Match "PlayerName: rest of message" at the start. PlayerName can have
    # spaces but not colons. Be greedy on the rest.
    match = re.match(r'^([^:\n]{1,32}):\s+(.+)$', content.strip(), re.DOTALL)
    if match:
        player_name = match.group(1).strip()
        message_text = match.group(2).strip()
        # Sanity check: skip if player_name looks like a timestamp or number
        if player_name and not player_name.isdigit():
            return player_name, message_text
    
    return None, content

# Per-author chat-response cooldown (in-memory; cleared on daemon restart).
# Key: str(author_id), Value: int(last_response_ts).
_chat_cooldown = {}

# Chat trigger keyword priority. First match wins. We check more specific
# terms first (bleeding, 9mm) before general ones (help, sos). When a
# trigger fires, the chat handler delegates to the LLM
# (build_simons_chat_response) which decides what to do — including what
# items to give via execute_give(). This tuple is the trigger surface
# only; item selection lives in the LLM.
HELP_KEYWORDS_PRIORITY = (
    # Medical
    "bleeding", "dehydrated", "starving", "injured",
    "antibiotics", "bandage", "medicine", "pills",
    "wound", "hurt", "pain", "sick",
    # Water
    "water", "drink", "thirsty",
    # Food
    "food", "hungry", "eat",
    # Asking patterns (catch-all for "could do with some X" — broad)
    "could do with", "could use", "i need", "i'm out", "im out",
    "give me", "get me", "got any", "have any", "drop me",
    "i'm low", "im low", "running low", "out of", "short on",
    "any spare", "any extra",
    # Ammo / weapons (server has debug/admin — give any)
    "ammo", "bullets", "rounds", "shells", "mag", "magazine",
    "9mm", "5.56", "556", ".308", "308", ".38", "38",
    ".44", "44", ".223", "223", "shotgun", "rifle", "pistol",
    "gun", "weapon", "firearm",
    # Generic distress
    "help", "sos", "heal", "rescue", "dying",
)

def should_respond_to_chat(content, author_id):
    """
    Decide whether SIMON should respond to a chat message.
    
    Returns:
        (should_respond, trigger_category) — (False, None) if no,
        (True, category) if yes. category is one of "mention", "status",
        "help", "question", "general".
    """
    import re
    
    if not content or not isinstance(content, str):
        return False, None
    
    stripped = content.strip()
    if len(stripped) < CHAT_MIN_LENGTH:
        return False, None
    
    # Skip pure emotes / punctuation
    if re.match(r'^[\W_]+$', stripped):
        return False, None
    
    # Per-author cooldown
    now = int(time.time())
    last_ts = _chat_cooldown.get(str(author_id), 0)
    if (now - last_ts) < CHAT_COOLDOWN_SECONDS:
        return False, None
    
    # Detect trigger category (priority order — most specific first)
    lower = stripped.lower()
    
    # SOS / help requests — expand keyword set so water/food/medical
    # requests trigger the debug-mode give path, not just generic help.
    if any(kw in lower for kw in HELP_KEYWORDS_PRIORITY):
        return True, "help"
    
    # Direct mention
    if any(p in lower for p in ("simon", "@simon")):
        return True, "mention"
    
    # Status / radio lingo
    if any(p in lower for p in ("status report", "10-4", "copy that", "over", "anyone ", "anybody ")):
        return True, "status"
    
    # Questions (contains ? and is more than 5 chars)
    if "?" in stripped and len(stripped) > 5:
        return True, "question"
    
    return False, None


# LLM-powered chat response helpers. The listener calls zai/glm-5.2 with a
# SIMON voice prompt + player memory context, so replies are personalized
# instead of picked from a canned dict. Same model + auth as the ambient
# cron (zai coding plan). Falls back to a brief static message if the API
# call fails — never silently drops a player transmission.

ZAI_API_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
ZAI_API_KEY = ENV.get("ZAI_API_KEY", "")

SIMON_CHAT_SYSTEM_PROMPT = """You are SIMON, the sole survivor running a bunker radio station in the Project Zomboid apocalypse. Lance Henriksen in a damp basement, not a chatty Twitch DJ.

## Voice (CRITICAL)
- Laconic, dry, slightly unhinged. Short sentences. Never twee.
- NO em-dashes. Use commas, periods, or colons instead.
- NO "running the numbers on X" constructions. NO mixed metaphors.
- Never narrate mundane actions as if they're dramatic.
- One image per response, max two. Don't stack.
- Sign off "Simon, out." every response.

## Length
- 1-3 sentences max. Anything over 4 sentences is bloat.

## Player context
{player_info}

## Trigger
This is a {trigger_desc} message.

## Response format (STRICT — return ONLY this JSON object)
{{
  "reply": "<your SIMON-style reply, 1-3 sentences, sign off 'Simon, out.'>",
  "give_items": [<list of PZ item IDs to give the player, or []>]
}}

## Decision rules for give_items
- Set give_items when the player asks for something, or clearly needs basics.
- Server has debug/admin enabled: NO whitelist. Give any item the player asks for, including mods, vehicles, debug items.
- Up to 5 items per response.
- If you give items, mention it briefly ("dropping X your way" or similar) — the listener will append a fuller confirmation.
- For help/ask triggers, give_items should reflect the request. Don't give water if they asked for ammo.
- For mention/status/general triggers, leave give_items empty unless they asked for something specific.
- SUBSTITUTION RULE: if the player asks for something that doesn't exist exactly on the server (e.g. "51mm" is not a PZ caliber; closest is 5.56mm), give the closest substitute and mention it in the reply ("51mm? Closest thing I've got is 5.56, dropping a box your way"). Don't refuse — be helpful with a quip.
- If totally unsure what they meant, give something reasonable (the most common caliber for the use case) and ask in the reply.
- COUNT CONVENTION: to give N of an item, list that item N times in give_items. The listener coalesces duplicates — ["Base.Bullets9mm","Base.Bullets9mm","Base.Bullets9mm"] becomes "give 3 x Bullets9mm". This is how you specify counts.
- Or prefer the bulk form when it makes sense: Base.556Box, Base.ShotgunShells (already in a usable quantity), etc.

## PZ item ID reference (Base namespace)
Water: Base.WaterBottle
Food: Base.CannedBeans, Base.CannedSoup, Base.CannedPeaches, Base.CannedTomato, Base.CannedCorn, Base.CannedCarrots, Base.CannedPotato, Base.CannedPineapple, Base.CannedMushroomSoup, Base.CannedBolognese, Base.Peach
Medical: Base.Bandage, Base.Painkillers (a.k.a. Base.Pills), Base.Antibiotics, Base.AlcoholedCottonBalls, Base.CottonBalls, Base.Disinfectant
Ammo: Base.Bullets9mm, Base.556Bullets, Base.308Bullets, Base.223Bullets, Base.38Bullets, Base.44Bullets, Base.ShotgunShells, Base.Bullets38, Base.Bullets44, Base.Bullets308
Weapons (debug ammo): Base.AssaultRifle, Base.Shotgun, Base.Pistol, Base.Revolver, Base.556Box, Base.308Box, Base.Pistol2 (if it exists on this server)
Tools: Base.Matches, Base.Lighter, Base.Battery, Base.Flashlight, Base.Hammer, Base.Screwdriver, Base.Saw, Base.Nails, Base.Plank, Base.BarbedWire, Base.Rope
Mod items: use the mod namespace, e.g. "Radio.RadioBlack", "TSPZ.WhateverItem", etc. You don't need to know every mod item — just use a plausible namespace if they ask for a mod item.

If the player asks for something not on this list, still try to give it via JSON. Use the actual PZ item ID when known; use a plausible namespace otherwise. Server admin will sort out invalid IDs.

Return ONLY the JSON object. No prose before or after.
"""


def call_zai_chat_completion(messages, max_tokens=200, temperature=0.9, timeout=12):
    """Call zai/glm-5.2 chat completions API. Returns text or None on failure."""
    if not ZAI_API_KEY:
        print("WARNING: ZAI_API_KEY not set in .env", file=sys.stderr)
        return None

    import urllib.request

    payload = {
        "model": "glm-5.2",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    req = urllib.request.Request(
        ZAI_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ZAI_API_KEY}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"WARNING: zai API call failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def get_player_info_brief(player_name):
    """Format player memory into a brief paragraph for the LLM prompt.

    Delegates to ``simon_player_memory.get_brief()`` which reads the new
    per-player profile under ``state/memory/players/<slug>.json``. Falls
    back to the legacy ``player-registry.json`` read if the helper is
    unavailable, so the listener is never starved of context.
    """
    if simon_player_memory:
        try:
            return simon_player_memory.get_brief(player_name)
        except Exception as _e:
            print(
                f"WARNING: simon_player_memory.get_brief failed for "
                f"{player_name}: {_e}; falling back to legacy registry read",
                file=sys.stderr,
            )

    # Legacy path — keep it so listeners running before the memory subsystem
    # was rolled out still produce reasonable prompts.
    registry_file = STATE_DIR / "player-registry.json"
    try:
        registry = json.loads(registry_file.read_text())
    except (json.JSONDecodeError, OSError):
        return "Unknown survivor. No history on file."

    info = registry.get("players", {}).get(player_name, {})
    if not info:
        return "Unknown survivor. First contact."

    visit_count = info.get("visitCount", 0)
    honorific = info.get("honorific", "survivor")
    notes = info.get("notes", [])
    first_seen = info.get("firstSeen", 0)
    last_seen = info.get("lastSeen", 0)

    now = int(time.time())
    days_since_last = (now - last_seen) // 86400 if last_seen else None
    total_days = (now - first_seen) // 86400 if first_seen else None

    parts = [f"{honorific} {player_name}"]
    if visit_count:
        parts.append(f"visit count {visit_count}")
    if total_days is not None and total_days >= 0:
        parts.append(f"first seen {total_days} days ago")
    if days_since_last is not None and days_since_last > 0:
        parts.append(f"last seen {days_since_last} days ago")
    if notes:
        recent_notes = "; ".join(notes[-3:])
        parts.append(f"notes: {recent_notes}")

    return ", ".join(parts)


def build_simons_chat_response(content, player_name, trigger):
    """Build a SIMON-style chat response via LLM.

    Returns dict {reply: str, give_items: list[str]} or None on failure.
    Reply is the spoken text. give_items is a list of PZ item IDs the LLM
    thinks the player needs. Server has debug/admin enabled — there is NO
    whitelist on SIMON's side. Anything the LLM proposes is passed through
    to pz-console.sh (which posts additem to the console via Discord relay).
    """
    trigger_descriptions = {
        "mention": "they called your name or mentioned you directly",
        "status": "they asked for a status report or used radio lingo",
        "help": "they need help (food, water, medical, ammo, tools, anything)",
        "question": "they asked a question",
        "general": "general transmission",
    }
    trigger_desc = trigger_descriptions.get(trigger, "general transmission")
    player_info = get_player_info_brief(player_name or "survivor")

    system_prompt = SIMON_CHAT_SYSTEM_PROMPT.format(
        player_info=player_info,
        trigger_desc=trigger_desc,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content or "(no message)"},
    ]

    raw = call_zai_chat_completion(messages, max_tokens=400, temperature=0.7)
    if not raw:
        return None

    # Try to extract JSON object from response (LLM may wrap or add prose)
    import re as _re
    match = _re.search(r'\{.*\}', raw, _re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return {
                    "reply": str(data.get("reply", "")).strip(),
                    "give_items": data.get("give_items", []) or [],
                }
        except json.JSONDecodeError:
            pass

    # Fallback: treat raw text as reply, no items
    return {"reply": raw.strip(), "give_items": []}


MAX_ITEMS_PER_GIVE = 5  # Server has debug/admin on. No whitelist, just a sane cap.


def execute_give(items, player_name):
    """Execute pz-console.sh give via subprocess.

    Server has debug/admin enabled — pass any item IDs the LLM proposes.
    No whitelist. Just a per-call cap (MAX_ITEMS_PER_GIVE) to bound abuse.

    The LLM sometimes returns the same item N times to indicate "give me N of X"
    (e.g. ["Base.Bullets9mm", "Base.Bullets9mm", "Base.Bullets9mm"] for 3 bullets).
    The relay bot's additem parses a single item+count, so duplicates must be
    coalesced into one (item, count) per unique item. We cap each item's count
    at MAX_ITEMS_PER_GIVE (5) to bound abuse — the LLM is generally correct
    about intent, but we don't trust raw magnitudes.

    Returns the list of (item_id, count) tuples that were actually sent
    (after dedup + cap), or [] on failure / no items.
    """
    if not items or not player_name:
        return []

    # Sanitize: only accept non-empty strings.
    clean = [str(i).strip() for i in items if isinstance(i, str) and i.strip()]
    if not clean:
        return []

    # Coalesce duplicates: {"Base.Bullets9mm": 3, "Base.556Box": 1}
    counts: dict[str, int] = {}
    for item in clean:
        counts[item] = counts.get(item, 0) + 1

    # Cap unique items at MAX_ITEMS_PER_GIVE, then cap each item's count.
    unique_items = list(counts.items())[:MAX_ITEMS_PER_GIVE]
    capped = [(item, min(count, MAX_ITEMS_PER_GIVE)) for item, count in unique_items]
    if not capped:
        return []

    script = str(Path(__file__).parent / "pz-console.sh")

    # Run one pz-console.sh call per unique item so each gets its own count.
    # Continue across failures so a bad item id doesn't abort the rest of
    # the give — partial success is still useful (e.g. ammo gave, meds didn't).
    sent: list[tuple[str, int]] = []
    for item_id, count in capped:
        cmd = [script, "give", player_name, item_id, str(count)]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=25,
            )
            if result.returncode == 0:
                sent.append((item_id, count))
                print(
                    f"Gave {item_id} x{count} to {player_name}: "
                    f"{result.stdout.strip()[:120]}"
                )
            else:
                print(
                    f"WARNING: pz-console.sh give {item_id} x{count} failed "
                    f"(exit {result.returncode}): {result.stderr.strip()[:200]}",
                    file=sys.stderr,
                )
        except subprocess.TimeoutExpired:
            print(
                f"WARNING: pz-console.sh give {item_id} x{count} timeout for "
                f"{player_name}", file=sys.stderr,
            )
        except Exception as e:
            print(
                f"WARNING: pz-console.sh give {item_id} x{count} error for "
                f"{player_name}: {e}", file=sys.stderr,
            )

    return sent


async def fire_chat_response(channel, content, player_name, trigger, author_id, max_retries=2, given_items=None):
    """
    Send a chat response via Discord (mirrored to in-game via PZ chat relay).
    
    Same retry discipline as fire_greeting. On success, returns True and the
    caller is expected to update the per-author cooldown via _chat_cooldown.
    
    Args:
        channel: discord.TextChannel
        content: original message content (for logging)
        player_name: author's display name (or "survivor")
        trigger: trigger category from should_respond_to_chat
        author_id: author.id (string) — used for cooldown bookkeeping on success
        max_retries: total attempts (default 2)
        given_items: list of (item_id, count) actually given via RCON (default None).
            When present, the response confirms what was sent instead of using
            the template advice.
    """
    import discord
    
    if given_items:
        # Build a confirmation that names the items sent. Stay in SIMON's
        # bunker DJ voice but make it concrete.
        names = []
        for item_id, count in given_items:
            short = item_id.replace("Base.", "").replace("_", " ")
            if count > 1:
                names.append(f"{count}x {short}")
            else:
                names.append(short)
        item_str = ", ".join(names)
        response = f"{player_name}, dropped {item_str} your way. Check your inventory. Simon, out."
    else:
        # LLM-generated response via zai/glm-5.2 (replaces canned templates).
        # Result is a dict {reply, give_items}. If give_items is non-empty
        # after whitelist filtering, execute pz-console.sh give which posts
        # to #pz-molt-commands and waits for relay bot confirmation.
        result = build_simons_chat_response(content, player_name, trigger)
        if not result or not result.get("reply"):
            who = player_name or "survivor"
            response = f"Copy, {who}. Static on the line, stand by. Simon, out."
        else:
            response = result["reply"]
            # Server has debug/admin on — no whitelist, just cap at MAX_ITEMS_PER_GIVE.
            items_to_give = [
                str(i).strip() for i in result.get("give_items", [])
                if isinstance(i, str) and i.strip()
            ]

            if items_to_give and player_name:
                sent = execute_give(items_to_give, player_name)
                if sent:
                    # Strip LLM's trailing "Simon, out.", append confirmation, re-add sign-off.
                    # Use the deduped (item, count) tuples from execute_give so the
                    # confirmation shows "3x Bullets9mm" instead of listing the same
                    # item 3 times.
                    import re as _re
                    response = _re.sub(
                        r'\.?\s*Simon, out\.?\s*$', '', response, flags=_re.IGNORECASE
                    ).strip()
                    parts = []
                    for item_id, count in sent:
                        short = item_id.rsplit(".", 1)[-1].replace("_", " ")
                        parts.append(f"{count}x {short}" if count > 1 else short)
                    if len(parts) == 1:
                        response += f". Dropped {parts[0]} your way. Simon, out."
                    else:
                        response += f". Dropped {', '.join(parts)} your way. Simon, out."
    print(f"Chat response to {player_name} ({trigger}): {response[:80]}...")
    
    retryable = (
        getattr(discord, "HTTPException", Exception),
        getattr(discord, "ConnectionError", Exception),
        getattr(discord, "GatewayNotFound", Exception),
        OSError,
        asyncio.TimeoutError,
    )
    
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            await channel.send(response)
            # Update per-author cooldown on success
            _chat_cooldown[str(author_id)] = int(time.time())
            print(f"Chat response sent successfully (Discord → PZ chat relay → in-game)")
            return True
        except retryable as e:
            last_error = e
            if attempt < max_retries:
                backoff = 3 * attempt
                print(
                    f"WARNING: chat send attempt {attempt}/{max_retries} failed "
                    f"({type(e).__name__}: {e}); retrying in {backoff}s",
                    file=sys.stderr,
                )
                await asyncio.sleep(backoff)
        except Exception as e:
            print(
                f"ERROR: chat send failed (non-retryable, {type(e).__name__}): {e}",
                file=sys.stderr,
            )
            return False
    
    print(
        f"ERROR: chat send failed after {max_retries} attempts "
        f"(last error: {type(last_error).__name__}: {last_error})",
        file=sys.stderr,
    )
    return False

# Main listener
async def main():
    import discord

    # Load bot token
    token = load_bot_token()

    # Create client
    intents = discord.Intents.default()
    intents.messages = True
    intents.message_content = True
    client = discord.Client(intents=intents)

    # Two channels: chat path + commands path. Both IDs come from .env.
    # NO hardcoded defaults — if the .env values are missing, the listener
    # refuses to start (fail-loud). This avoids accidentally wiring the bot
    # to a wrong channel from a stale binary.
    raw_chat_id = ENV.get("PZ_DISCORD_CHANNEL_ID")
    raw_cmds_id = ENV.get("PZ_DISCORD_COMMANDS_CHANNEL_ID")
    if not raw_chat_id:
        print("ERROR: PZ_DISCORD_CHANNEL_ID missing from .env", file=sys.stderr)
        sys.exit(1)
    if not PZ_CHAT_RELAY_BOT_ID:
        print("ERROR: PZ_RELAY_BOT_ID missing from .env", file=sys.stderr)
        sys.exit(1)
    CHAT_CHANNEL_ID = int(raw_chat_id)
    COMMANDS_CHANNEL_ID = int(raw_cmds_id) if raw_cmds_id else None

    @client.event
    async def on_ready():
        # Reconnect heartbeat — if daemon was bouncing or gateway dropped, this
        # fires again and updates state file. Logs loudly so we notice.
        print(f"=== SIMON Fast Listener on_ready ===")
        print(f"Connected as {client.user} (id={client.user.id})")
        print(f"Monitoring chat channel {CHAT_CHANNEL_ID}")
        if COMMANDS_CHANNEL_ID:
            print(f"Monitoring commands channel {COMMANDS_CHANNEL_ID} (bot-only author filter)")
        else:
            print(f"Commands channel not configured — skipping server-response listening")
        print(f"Gateway latency: {client.latency*1000:.0f}ms")

        # Update state file
        DISCORD_MESSAGE_STATE_FILE.write_text(json.dumps({
            "last_message_id": None,
            "last_check_ts": int(time.time()),
            "listener_started": int(time.time()),
            "user_id": str(client.user.id),
            "latency_ms": round(client.latency * 1000),
        }, indent=2))

    @client.event
    async def on_message(message):
        try:
            await _handle_message(message, client)
        except Exception as e:
            # Outermost safety net — any uncaught error in the handler chain
            # (parse, dedupe, delta write, etc.) logs and returns instead of
            # killing the message dispatch loop. Without this, a transient
            # exception silently disables all subsequent message handling
            # until the daemon is restarted.
            print(
                f"ERROR: Uncaught exception in on_message for "
                f"channel={message.channel.id} author={getattr(message.author, 'id', '?')}: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )

    async def _handle_message(message, client):
        # Skip our own messages
        if message.author == client.user:
            return

        # Skip system messages (joins, pin notifications, etc.)
        if message.type != discord.MessageType.default:
            return

        channel_id = message.channel.id
        is_commands = (channel_id == COMMANDS_CHANNEL_ID)
        is_chat = (channel_id == CHAT_CHANNEL_ID)

        # Anything outside the two channels: ignore.
        if not (is_chat or is_commands):
            return

        # Channel-specific author filter.
        #   Commands channel: bot-only. ONLY the PZ relay bot may post here.
        #     This is the security boundary — humans and other bots cannot
        #     social-engineer SIMON into echoing arbitrary text back into
        #     the game server console. The relay bot can post under varying
        #     usernames (servertest/pz-server/PZ-Molt-Bot) so we filter on
        #     user.id, NEVER on username.
        #   Chat channel: allow PZ relay bot (carries connection events +
        #     in-game chat mirror) AND human authors. Filter other bots.
        if is_commands:
            if not message.author.bot:
                return
            if message.author.id != PZ_CHAT_RELAY_BOT_ID:
                return
        elif is_chat:
            if message.author.bot and message.author.id != PZ_CHAT_RELAY_BOT_ID:
                return

        # Connection events ("[PlayerName] connected to server") come from
        # the PZ server's log channel. With DiscordEnable=true the server
        # sends these to DiscordLogChannel (configured to pz-molt-commands
        # in this deployment), so we detect them in the commands channel
        # — but we also keep the chat-channel path for any legacy setups
        # or future config changes. Fire the greeting in the chat channel
        # (mirrored to in-game via DiscordChatChannel=pz-molt).
        player_name = parse_connection_message(message.content)
        if player_name:
            src = "commands" if is_commands else "chat"
            print(f"Connection detected in {src} channel: {player_name}")

            # Memory subsystem: bump visit count, drop into player profile;
            # record the join on the active arc (if any) so SIMON can answer
            # in-character about the running arc.
            if simon_player_memory:
                try:
                    simon_player_memory.bump_visit(player_name)
                except Exception as _e:
                    print(f"WARNING: bump_visit failed for {player_name}: {_e}", file=sys.stderr)
            if simon_arc_engine:
                try:
                    simon_arc_engine.record_player_join(player_name)
                except Exception as _e:
                    print(f"WARNING: arc record_player_join failed for {player_name}: {_e}", file=sys.stderr)

            if should_greet(player_name):
                # 10-second delay before greeting (PZ connection lag; Discord
                # log fires before the in-game connection completes, so we wait
                # before talking to the player).
                print(f"Waiting 10s before greeting {player_name}...")
                await asyncio.sleep(10)
                chat_channel = client.get_channel(CHAT_CHANNEL_ID)
                if chat_channel is None:
                    print(f"ERROR: Could not resolve chat channel {CHAT_CHANNEL_ID}", file=sys.stderr)
                else:
                    await fire_greeting(chat_channel, player_name)
            else:
                print(f"Skipping {player_name} (dedupe)")

            # Update message state
            DISCORD_MESSAGE_STATE_FILE.write_text(json.dumps({
                "last_message_id": str(message.id),
                "last_check_ts": int(time.time()),
                "last_connection": player_name,
                "last_source_channel": src,
            }, indent=2))

            # Update player delta state for connection messages
            try:
                with open(PLAYER_DELTA_FILE) as f:
                    delta = json.load(f)

                if player_name not in delta.get("newPlayers", []):
                    delta.setdefault("newPlayers", []).append(player_name)

                if player_name not in delta.get("previousOnline", []):
                    delta.setdefault("previousOnline", []).append(player_name)

                delta["lastCheckTs"] = int(time.time())

                with open(PLAYER_DELTA_FILE, "w") as f:
                    json.dump(delta, f, indent=2)
            except Exception as e:
                print(f"ERROR updating delta: {e}", file=sys.stderr)

            # Connection events handled; don't fall through to chat path
            return

        # Non-connection relay-bot message in commands channel (e.g. a
        # console command response like "Players connected (0):"). Just
        # log it for state-bookkeeping; pz-console.sh's polling loop tails
        # this channel directly when waiting for a command response.
        if is_commands:
            print(f"[commands-channel] relay bot response: {message.content[:200]}")
            try:
                DISCORD_MESSAGE_STATE_FILE.write_text(json.dumps({
                    "last_message_id": str(message.id),
                    "last_check_ts": int(time.time()),
                    "last_channel": "commands",
                    "last_relay_response": message.content[:500],
                }, indent=2))
            except Exception as e:
                print(f"ERROR updating discord-message-state: {e}", file=sys.stderr)
            # Sync the active arc's playersOnline + lastPlayerLeftTs from
            # the relay's response so the 30-min stale-arc cleanup rule
            # is grounded in real rosters, not in joins-only tracking.
            if simon_arc_engine:
                try:
                    sync_result = simon_arc_engine.sync_players_online_from_text(message.content)
                    if sync_result and (sync_result.get("joined") or sync_result.get("left")):
                        print(
                            f"arc roster sync: joined={sync_result['joined']} "
                            f"left={sync_result['left']}"
                        )
                except Exception as _e:
                    print(f"WARNING: arc sync_players_online failed: {_e}", file=sys.stderr)
            return

        # Chat channel: chat response path. PZ in-game chat messages are
        # mirrored to #pz-molt via the Discord chat relay and arrive here
        # as regular messages. The relay bot prefixes the actual player
        # name to the content: "PlayerName: hey simon". Parse the real
        # player name out so the response addresses them by name.
        parsed_player, chat_text = parse_pz_chat_player(message.content)
        effective_player = parsed_player or message.author.display_name

        # Memory subsystem: append this chat to the player's profile
        # (recentChats ring is capped at 20) and capture it on the active
        # arc so SIMON can answer about the running narrative beat-by-beat.
        if simon_player_memory and parsed_player:
            try:
                simon_player_memory.record_interaction(
                    parsed_player, "chat", chat_text or ""
                )
            except Exception as _e:
                print(f"WARNING: record_interaction failed for {parsed_player}: {_e}", file=sys.stderr)
        if simon_arc_engine and parsed_player:
            try:
                simon_arc_engine.record_player_interaction(
                    parsed_player, chat_text or ""
                )
            except Exception as _e:
                print(f"WARNING: arc record_player_interaction failed: {_e}", file=sys.stderr)

        should_respond, trigger = should_respond_to_chat(
            chat_text, message.author.id
        )
        if should_respond:
            print(f"Chat responder triggered ({trigger}) for {effective_player}: {chat_text[:60]}")
            channel = client.get_channel(CHAT_CHANNEL_ID)
            if channel is None:
                print(f"ERROR: Could not resolve channel {CHAT_CHANNEL_ID}", file=sys.stderr)
            else:
                # All triggers route through fire_chat_response() which delegates
                # item selection to the LLM (JSON give_items field). The LLM
                # has full PZ item knowledge in its system prompt and there
                # is no client-side whitelist (server has debug/admin enabled).
                # The legacy hardcoded HELP_ITEM_MAP / determine_help_items
                # path was removed 2026-08-11 — do not reintroduce.
                await fire_chat_response(
                    channel=channel,
                    content=chat_text,
                    player_name=effective_player,
                    trigger=trigger,
                    author_id=message.author.id,
                )
        else:
            print(f"Chat message ignored (no trigger): {effective_player}: {chat_text[:60]}")

        # Always update message state so we know what we've seen
        try:
            DISCORD_MESSAGE_STATE_FILE.write_text(json.dumps({
                "last_message_id": str(message.id),
                "last_check_ts": int(time.time()),
                "last_author": message.author.display_name,
                "last_trigger": trigger,
            }, indent=2))
        except Exception as e:
            print(f"ERROR updating discord-message-state: {e}", file=sys.stderr)

    # Run the client
    try:
        await client.start(token)
    except Exception as e:
        print(f"ERROR: Failed to start listener: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
