#!/usr/bin/env node
/**
 * Trigger gate for the SIMON Ambient Director cron job.
 * JS port of simon_ambient_trigger.py — code-mode only supports javascript/
 * typescript, so the Python version was failing with SyntaxError: invalid
 * regular expression flags when code-mode tried to TS-parse the Python source.
 *
 * Detection rules (any one fires):
 *   1. Recent "connected to server" event (someone joined)
 *   2. Recent "Players connected (N)" with N >= 1 (anyone online)
 *
 * Otherwise: skip. Empty server = no LLM cost.
 *
 * Side-effects before the fire decision (runs on EVERY tick, including the
 * not-firing path):
 *   - syncs the active arc's playersOnline/lastPlayerLeftTs from the listener
 *     cache (so the 30-min stale-arc cleanup rule is grounded in real rosters)
 *   - finalizes orphan arcs (no players for >30 min) — abandoned_no_players_30m
 *
 * Housekeeping runs even when the trigger returns false; orphan arcs get
 * cleaned up regardless of whether the LLM payload fires.
 */
const fs = require('fs');
const path = require('path');

const SKILL_DIR = path.resolve(__dirname, '..');
const STATE_FILE = path.join(SKILL_DIR, 'state', 'discord-message-state.json');
const ARCS_DIR = path.join(SKILL_DIR, 'state', 'memory', 'arcs');
const ACTIVE_FILE = path.join(ARCS_DIR, 'active.json');
const INDEX_FILE = path.join(ARCS_DIR, 'index.json');
const MAX_AGE_SECONDS = 900; // 15 minutes — within one cron tick window
const STALE_PLAYER_GRACE_SECONDS = 30 * 60; // 30 minutes for stale-arc cleanup

// ---------------------------------------------------------------------------
// Listener cache read
// ---------------------------------------------------------------------------
let lastResp = '';
let lastTs = 0;
try {
  if (fs.existsSync(STATE_FILE)) {
    const state = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
    lastResp = String(state.last_relay_response || '');
    lastTs = Number(state.last_check_ts || 0);
  }
} catch (_e) {
  lastResp = '';
  lastTs = 0;
}

// ---------------------------------------------------------------------------
// Arc housekeeping (runs every tick, even when not firing)
// ---------------------------------------------------------------------------
function readJSON(p, fallback) {
  try {
    if (!fs.existsSync(p)) return fallback;
    return JSON.parse(fs.readFileSync(p, 'utf8'));
  } catch (_e) {
    return fallback;
  }
}

function writeJSON(p, data) {
  try {
    fs.writeFileSync(p, JSON.stringify(data, null, 2));
  } catch (_e) {
    // best-effort
  }
}

function syncPlayersOnlineFromText(text) {
  // Mirror simon_arc_engine.sync_players_online_from_text.
  // Updates active.json's playersOnline + lastPlayerLeftTs from a relay-bot
  // response like "Players connected (3):\nStone, Sarah, Mike".
  if (!text) return;
  const arc = readJSON(ACTIVE_FILE, null);
  if (!arc) return;
  const m = text.match(/Players\s+connected\s+\((\d+)\)\s*:?\s*((?:\n?[A-Za-z0-9_\- ,]{1,64})*)/i);
  if (!m) return;
  const rawNames = m[2] || '';
  const names = rawNames.split(/[,\n]/).map(s => s.trim()).filter(Boolean);
  arc.playersOnline = names;
  if (names.length === 0) {
    if (!arc.lastPlayerLeftTs) arc.lastPlayerLeftTs = Math.floor(Date.now() / 1000);
  } else {
    arc.lastPlayerLeftTs = null;
  }
  writeJSON(ACTIVE_FILE, arc);
}

function cleanupStaleArc() {
  // Mirror simon_arc_engine.cleanup_stale_arc — finalize orphan arcs as
  // abandoned_no_players_30m when empty for >30 min.
  const arc = readJSON(ACTIVE_FILE, null);
  if (!arc) return;
  const online = Array.isArray(arc.playersOnline) ? arc.playersOnline : [];
  const lastLeft = arc.lastPlayerLeftTs || null;
  if (online.length > 0 || !lastLeft) return;
  const ageSec = Math.floor(Date.now() / 1000) - lastLeft;
  if (ageSec < STALE_PLAYER_GRACE_SECONDS) return;
  // Finalize
  arc.finalizeReason = 'abandoned_no_players_30m';
  arc.finalizedAtTs = Math.floor(Date.now() / 1000);
  const idx = readJSON(INDEX_FILE, { _about: 'completed arcs index', arcs: [], schemaVersion: 1 });
  if (!Array.isArray(idx.arcs)) idx.arcs = [];
  idx.arcs.push(arc);
  if (idx.arcs.length > 50) idx.arcs = idx.arcs.slice(-50);
  writeJSON(INDEX_FILE, idx);
  try {
    fs.unlinkSync(ACTIVE_FILE);
  } catch (_e) {
    // best-effort
  }
}

try { syncPlayersOnlineFromText(lastResp); } catch (_e) {}
try { cleanupStaleArc(); } catch (_e) {}

// ---------------------------------------------------------------------------
// Fire-decision
// ---------------------------------------------------------------------------
const now = Math.floor(Date.now() / 1000);
let fire = false;

if (lastTs === 0 || (now - lastTs) > MAX_AGE_SECONDS) {
  fire = false;
} else if (lastResp.indexOf('connected to server') !== -1) {
  fire = true;
} else {
  const m = lastResp.match(/Players connected \((\d+)\)/);
  if (m && Number(m[1]) >= 1) fire = true;
}

process.stdout.write(JSON.stringify({ fire }) + '\n');
process.exit(0);