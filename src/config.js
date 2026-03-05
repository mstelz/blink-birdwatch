import fs from 'node:fs';
import path from 'node:path';

function unquote(value) {
  const trimmed = value.trim();
  if ((trimmed.startsWith('"') && trimmed.endsWith('"')) || (trimmed.startsWith("'") && trimmed.endsWith("'"))) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function numberFromEnv(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

export function loadEnvFile() {
  const envPath = path.resolve(process.cwd(), '.env');
  if (!fs.existsSync(envPath)) return;

  const lines = fs.readFileSync(envPath, 'utf8').split('\n');
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const idx = trimmed.indexOf('=');
    if (idx === -1) continue;

    const key = trimmed.slice(0, idx).trim();
    const value = unquote(trimmed.slice(idx + 1));
    if (!(key in process.env)) process.env[key] = value;
  }
}

export function getConfig() {
  return {
    port: numberFromEnv(process.env.PORT, 8787),
    pollIntervalSec: numberFromEnv(process.env.POLL_INTERVAL_SEC, 180),
    blinkEventsFile: process.env.BLINK_EVENTS_FILE || './config/blink-events.json',
    workDir: process.env.WORK_DIR || './work',
    birdnetGoInputDir: process.env.BIRDNET_GO_INPUT_DIR || '/app/output'
  };
}
