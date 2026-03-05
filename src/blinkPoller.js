import { exec as execCb } from 'node:child_process';
import { promisify } from 'node:util';
import { loadEnvFile, getConfig } from './config.js';

const exec = promisify(execCb);

loadEnvFile();
const cfg = getConfig();

const bridgeUrl = process.env.BRIDGE_URL || `http://127.0.0.1:${cfg.port}/bridge/blink/event`;
const fetchCommand = process.env.BLINK_FETCH_COMMAND || '';
const parsedInterval = Number(process.env.BLINK_POLL_INTERVAL_SEC || cfg.pollIntervalSec || 180);
const intervalSec = Number.isFinite(parsedInterval) && parsedInterval > 0 ? parsedInterval : 180;

async function fetchEventsFromCommand() {
  if (!fetchCommand) {
    throw new Error('BLINK_FETCH_COMMAND is not set');
  }
  const { stdout } = await exec(fetchCommand, { maxBuffer: 10 * 1024 * 1024 });
  const parsed = JSON.parse(stdout || '[]');
  if (!Array.isArray(parsed)) throw new Error('BLINK_FETCH_COMMAND must output a JSON array');
  return parsed;
}

async function pushToBridge(event) {
  const res = await fetch(bridgeUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(event)
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`bridge push failed ${res.status}: ${text}`);
  }

  return res.json();
}

async function runOnce() {
  const events = await fetchEventsFromCommand();
  let added = 0;
  for (const event of events) {
    if (!event?.id) continue;
    const result = await pushToBridge(event);
    if (result?.added) added += 1;
  }
  console.log(`[blink-poller] events fetched=${events.length} newly-added=${added}`);
}

async function loop() {
  while (true) {
    try {
      await runOnce();
    } catch (err) {
      console.error(`[blink-poller] ${err.message || err}`);
    }
    await new Promise((r) => setTimeout(r, intervalSec * 1000));
  }
}

console.log(`[blink-poller] bridge=${bridgeUrl} interval=${intervalSec}s`);
loop();
