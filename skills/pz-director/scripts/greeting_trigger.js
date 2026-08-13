#!/usr/bin/env node
/**
 * Trigger gate for the SIMON Greeting Dispatcher cron job.
 * JS port of greeting_trigger.py — code-mode only supports javascript/typescript,
 * so the Python shebang version was getting parsed as JS and failing with
 * SyntaxError: invalid regular expression flags.
 *
 * Returns {"fire": true} only if state/greeting-queue.json has pending entries.
 * No Discord posts, no LLM calls — just a file read.
 *
 * Designed for a 1-minute cron schedule so greetings fire within ~60s of a
 * player connection event being queued by the listener.
 */
const fs = require('fs');
const path = require('path');
const QUEUE_FILE = path.resolve(__dirname, '..', 'state', 'greeting-queue.json');

let fire = false;
try {
  if (fs.existsSync(QUEUE_FILE)) {
    const queue = JSON.parse(fs.readFileSync(QUEUE_FILE, 'utf8'));
    const pending = Array.isArray(queue.pending) ? queue.pending : [];
    if (pending.length > 0) fire = true;
  }
} catch (_e) {
  fire = false;
}

process.stdout.write(JSON.stringify({ fire }) + '\n');
process.exit(0);