import express from 'express';
import fs from 'node:fs';
import path from 'node:path';
import { loadEnvFile, getConfig } from './config.js';
import { getBlinkEvents } from './providers/blinkFileProvider.js';
import { appendBlinkEvent } from './blinkBridge.js';
import { downloadFile, extractAudioFromVideo } from './utils.js';

loadEnvFile();
const cfg = getConfig();
const app = express();
app.use(express.json());

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

  if (!motion.mediaUrl) {
    console.error(`[bridge] skipping motion ${motion.id}: mediaUrl is missing`);
    return;
  }

  const stamp = `blink_${new Date(motion.timestamp || Date.now()).toISOString().replace(/[:.]/g, '-')}`;
  fs.mkdirSync(workDir, { recursive: true });
  const videoPath = path.join(workDir, `${stamp}.mp4`);
  const wavPath = path.join(workDir, `${stamp}.wav`);
  const outPath = path.join(cfg.birdnetGoInputDir, `${stamp}.wav`);

  try {
    await downloadFile(motion.mediaUrl, videoPath);
    await extractAudioFromVideo(videoPath, wavPath);
    fs.mkdirSync(cfg.birdnetGoInputDir, { recursive: true });
    fs.renameSync(wavPath, outPath);
    console.log(`[bridge] dropped ${path.basename(outPath)} into ${cfg.birdnetGoInputDir}`);
  } catch (err) {
    // allow retry in subsequent polls for transient failures
    seenMotionIds.delete(motion.id);
    persistSeenIds();
    console.error(`[bridge] failed to process motion ${motion.id}: ${err.message}`);
  } finally {
    try {
      if (fs.existsSync(videoPath)) fs.unlinkSync(videoPath);
    } catch {
      // best effort cleanup
    }
    try {
      if (fs.existsSync(wavPath)) fs.unlinkSync(wavPath);
    } catch {
      // best effort cleanup
    }
  }
}

async function processBlinkEvents() {
  const incoming = getBlinkEvents(cfg.blinkEventsFile);
  for (const motion of incoming) {
    await processMotionEvent(motion);
  }
}

app.get('/health', (_req, res) => {
  res.json({
    ok: true,
    pollIntervalSec: cfg.pollIntervalSec,
    birdnetGoInputDir: cfg.birdnetGoInputDir,
    seenEvents: seenMotionIds.size
  });
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

const pollTimer = setInterval(() => {
  processBlinkEvents().catch((e) => console.error('poll error', e.message));
}, cfg.pollIntervalSec * 1000);

const server = app.listen(cfg.port, () => {
  console.log(`blink-bridge running on :${cfg.port}`);
  console.log(`polling blink events from ${cfg.blinkEventsFile} every ${cfg.pollIntervalSec}s`);
  console.log(`dropping WAV files into ${cfg.birdnetGoInputDir}`);
});

function shutdown(signal) {
  console.log(`[bridge] received ${signal}, shutting down`);
  clearInterval(pollTimer);
  server.close(() => process.exit(0));
}

process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
