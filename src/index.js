import express from 'express';
import fs from 'node:fs';
import path from 'node:path';
import { exec as execCb } from 'node:child_process';
import { promisify } from 'node:util';
import { loadEnvFile, getConfig } from './config.js';
import { getBlinkEvents } from './providers/blinkFileProvider.js';
import { appendBlinkEvent } from './blinkBridge.js';
import { downloadFile, extractAudioFromVideo } from './utils.js';

loadEnvFile();
const cfg = getConfig();
const app = express();
app.use(express.json());

const exec = promisify(execCb);

async function runJsonCommand(command) {
  const { stdout, stderr } = await exec(command, { maxBuffer: 10 * 1024 * 1024 });
  const text = (stdout || '').trim();
  if (!text) throw new Error(`empty output${stderr ? `; stderr: ${stderr.trim()}` : ''}`);
  return JSON.parse(text);
}

async function getAuthStatus() {
  try {
    return await runJsonCommand('python3 /app/bin/blink_auth.py status');
  } catch (err) {
    return {
      ok: false,
      authenticated: false,
      needs_credentials: true,
      needs_2fa: false,
      locked_error: true,
      paused_fetch: true,
      last_error: err.message,
      last_attempt_at: null,
      next_allowed_attempt_at: null,
      auth_file: cfg.blinkAuthFile,
      db_file: cfg.blinkDbFile
    };
  }
}

const MAX_SEEN_IDS = 10_000;
const workDir = path.resolve(process.cwd(), cfg.workDir);
const seenIdsFile = path.join(workDir, '.seen-motion-ids.json');
const seenMotionIds = new Set();

function loadSeenIds() {
  try {
    if (!fs.existsSync(seenIdsFile)) return;
    const raw = fs.readFileSync(seenIdsFile, 'utf8');
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      for (const id of parsed) {
        if (typeof id === 'string' && id) seenMotionIds.add(id);
      }
    }
    if (seenMotionIds.size > 0) {
      console.log(`[bridge] loaded ${seenMotionIds.size} previously-seen event IDs`);
    }
  } catch (err) {
    console.error(`[bridge] failed to load seen IDs: ${err.message}`);
  }
}

function persistSeenIds() {
  try {
    fs.mkdirSync(workDir, { recursive: true });
    fs.writeFileSync(seenIdsFile, `${JSON.stringify([...seenMotionIds], null, 2)}\n`, 'utf8');
  } catch (err) {
    console.error(`[bridge] failed to persist seen IDs: ${err.message}`);
  }
}

function pruneSeenIds() {
  if (seenMotionIds.size <= MAX_SEEN_IDS) return;

  const excess = seenMotionIds.size - MAX_SEEN_IDS;
  const iter = seenMotionIds.values();
  for (let i = 0; i < excess; i++) iter.next();

  const keep = [];
  for (let r = iter.next(); !r.done; r = iter.next()) keep.push(r.value);

  seenMotionIds.clear();
  for (const id of keep) seenMotionIds.add(id);
}

async function processMotionEvent(motion) {
  if (!motion?.id || seenMotionIds.has(motion.id)) return;

  seenMotionIds.add(motion.id);
  pruneSeenIds();
  persistSeenIds();

  if (!motion.mediaUrl && !motion.localFile) {
    console.error(`[bridge] skipping motion ${motion.id}: mediaUrl/localFile is missing`);
    return;
  }

  const stamp = `blink_${new Date(motion.timestamp || Date.now()).toISOString().replace(/[:.]/g, '-')}`;
  fs.mkdirSync(workDir, { recursive: true });
  const videoPath = path.join(workDir, `${stamp}.mp4`);
  const wavPath = path.join(workDir, `${stamp}.wav`);
  const outPath = path.join(cfg.birdnetGoInputDir, `${stamp}.wav`);

  try {
    if (motion.localFile) {
      fs.copyFileSync(motion.localFile, videoPath);
    } else {
      await downloadFile(motion.mediaUrl, videoPath);
    }
    await extractAudioFromVideo(videoPath, wavPath);
    fs.mkdirSync(cfg.birdnetGoInputDir, { recursive: true });
    fs.renameSync(wavPath, outPath);
    console.log(`[bridge] dropped ${path.basename(outPath)} into ${cfg.birdnetGoInputDir}`);
  } catch (err) {
    seenMotionIds.delete(motion.id);
    persistSeenIds();
    console.error(`[bridge] failed to process motion ${motion.id}: ${err.message}`);
  } finally {
    try {
      if (fs.existsSync(videoPath)) fs.unlinkSync(videoPath);
    } catch {}
    try {
      if (fs.existsSync(wavPath)) fs.unlinkSync(wavPath);
    } catch {}
  }
}

async function processBlinkEvents() {
  const incoming = getBlinkEvents(cfg.blinkEventsFile);
  for (const motion of incoming) {
    await processMotionEvent(motion);
  }
}

async function pollBlinkFetchCommand() {
  if (!cfg.blinkFetchCommand) return;

  const auth = await getAuthStatus();
  if (!auth.ok || auth.paused_fetch || auth.locked_error || auth.needs_credentials || auth.needs_2fa) {
    const why = auth.last_error || (auth.needs_2fa ? 'needs 2FA' : auth.needs_credentials ? 'needs credentials' : 'paused/locked');
    console.log(`[bridge] blink fetch skipped (${why})`);
    return;
  }

  try {
    const { stdout } = await exec(cfg.blinkFetchCommand, { maxBuffer: 10 * 1024 * 1024 });
    const parsed = JSON.parse(stdout || '[]');
    if (!Array.isArray(parsed)) throw new Error('BLINK_FETCH_COMMAND must output a JSON array');

    let added = 0;
    for (const motion of parsed) {
      if (!motion?.id) continue;
      const appended = appendBlinkEvent(cfg.blinkEventsFile, motion);
      if (appended) {
        added += 1;
        await processMotionEvent(motion);
      }
    }

    console.log(`[bridge] blink fetch poll ran: returned ${parsed.length} event(s), added ${added}`);
  } catch (err) {
    console.error(`[bridge] BLINK_FETCH_COMMAND failed: ${err.message}`);
  }
}

app.get('/health', async (_req, res) => {
  const auth = await getAuthStatus();
  res.json({
    ok: true,
    pollIntervalSec: cfg.pollIntervalSec,
    birdnetGoInputDir: cfg.birdnetGoInputDir,
    seenEvents: seenMotionIds.size,
    auth
  });
});

app.get('/auth/status', async (_req, res) => {
  const status = await getAuthStatus();
  res.json(status);
});


app.post('/bridge/blink/event', async (req, res) => {
  const body = req.body || {};
  if (!body.id || typeof body.id !== 'string') {
    return res.status(400).json({ ok: false, error: 'id (string) is required' });
  }
  if (!body.mediaUrl || typeof body.mediaUrl !== 'string') {
    return res.status(400).json({ ok: false, error: 'mediaUrl (string) is required' });
  }

  const added = appendBlinkEvent(cfg.blinkEventsFile, body);
  if (added) await processMotionEvent(body);

  return res.json({ ok: true, added });
});

loadSeenIds();
processBlinkEvents().catch((e) => console.error('initial poll error', e.message));
if (cfg.blinkFetchCommand) {
  pollBlinkFetchCommand().catch((e) => console.error('initial fetch-command poll error', e.message));
}

const pollTimer = setInterval(() => {
  processBlinkEvents().catch((e) => console.error('poll error', e.message));
}, cfg.pollIntervalSec * 1000);

const fetchTimer = cfg.blinkFetchCommand
  ? setInterval(() => {
      pollBlinkFetchCommand().catch((e) => console.error('fetch-command poll error', e.message));
    }, cfg.blinkPollIntervalSec * 1000)
  : null;

const server = app.listen(cfg.port, () => {
  console.log(`blink-bridge running on :${cfg.port}`);
  console.log(`polling blink events from ${cfg.blinkEventsFile} every ${cfg.pollIntervalSec}s`);
  if (cfg.blinkFetchCommand) {
    console.log(`running BLINK_FETCH_COMMAND every ${cfg.blinkPollIntervalSec}s (lockout-safe mode)`);
  }
  console.log(`auth db: ${cfg.blinkDbFile}`);
  console.log(`dropping WAV files into ${cfg.birdnetGoInputDir}`);
});

function shutdown(signal) {
  console.log(`[bridge] received ${signal}, shutting down`);
  clearInterval(pollTimer);
  if (fetchTimer) clearInterval(fetchTimer);
  server.close(() => process.exit(0));
}

process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
