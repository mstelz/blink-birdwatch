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
let authLastError = '';

async function runJsonCommand(command) {
  const { stdout, stderr } = await exec(command, { maxBuffer: 10 * 1024 * 1024 });
  const text = (stdout || '').trim();
  if (!text) throw new Error(`empty output${stderr ? `; stderr: ${stderr.trim()}` : ''}`);
  return JSON.parse(text);
}

async function getAuthStatus() {
  try {
    const data = await runJsonCommand('python3 /app/bin/blink_auth.py status');
    if (!data.ok && data.error) authLastError = data.error;
    return { ...data, lastError: authLastError || undefined };
  } catch (err) {
    authLastError = err.message;
    return {
      ok: false,
      authenticated: false,
      needs2fa: false,
      hasCredentials: Boolean(process.env.BLINK_USERNAME && process.env.BLINK_PASSWORD),
      authFile: cfg.blinkAuthFile,
      lastError: authLastError
    };
  }
}

function requireBridgeToken(req, res, next) {
  if (!cfg.bridgeAuthToken) return next();
  const token = req.get('x-bridge-token');
  if (token !== cfg.bridgeAuthToken) {
    return res.status(401).json({ ok: false, error: 'unauthorized: missing/invalid X-Bridge-Token' });
  }
  return next();
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

async function pollBlinkFetchCommand() {
  if (!cfg.blinkFetchCommand) return;

  try {
    const { stdout } = await exec(cfg.blinkFetchCommand, { maxBuffer: 10 * 1024 * 1024 });
    const parsed = JSON.parse(stdout || '[]');
    if (!Array.isArray(parsed)) {
      throw new Error('BLINK_FETCH_COMMAND must output a JSON array');
    }

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

app.get('/health', (_req, res) => {
  res.json({
    ok: true,
    pollIntervalSec: cfg.pollIntervalSec,
    birdnetGoInputDir: cfg.birdnetGoInputDir,
    seenEvents: seenMotionIds.size
  });
});

app.get('/auth/status', async (_req, res) => {
  const status = await getAuthStatus();
  res.json(status);
});

app.post('/auth/login', requireBridgeToken, (req, res) => {
  const username = String(req.body?.username || '').trim();
  const password = String(req.body?.password || '').trim();
  if (!username || !password) {
    return res.status(400).json({ ok: false, error: 'username and password are required' });
  }

  try {
    fs.mkdirSync(path.dirname(cfg.blinkAuthFile), { recursive: true });
    fs.writeFileSync(cfg.blinkAuthFile, `${JSON.stringify({ username, password }, null, 2)}\n`, 'utf8');
    authLastError = '';
    return res.json({ ok: true, authFile: cfg.blinkAuthFile });
  } catch (err) {
    authLastError = err.message;
    return res.status(500).json({ ok: false, error: err.message });
  }
});

app.post('/auth/2fa', requireBridgeToken, async (req, res) => {
  const code = String(req.body?.code || '').trim();
  if (!code) return res.status(400).json({ ok: false, error: 'code is required' });

  try {
    const data = await runJsonCommand(`python3 /app/bin/blink_auth.py verify-2fa ${JSON.stringify(code)}`);
    if (!data.ok && data.error) authLastError = data.error;
    return res.status(data.ok ? 200 : 400).json({ ...data, lastError: authLastError || undefined });
  } catch (err) {
    authLastError = err.message;
    return res.status(500).json({ ok: false, error: err.message, lastError: authLastError });
  }
});

app.get('/auth', (_req, res) => {
  res.type('html').send(`<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Blink Bridge Auth</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 680px; margin: 2rem auto; padding: 0 1rem; }
    .row { margin: 0.75rem 0; }
    input { padding: 0.5rem; width: 100%; box-sizing: border-box; }
    button { padding: 0.55rem 0.8rem; }
    code { background: #f3f3f3; padding: 0.15rem 0.3rem; border-radius: 4px; }
    .ok { color: #0b7a0b; }
    .bad { color: #b42318; }
    .muted { color: #666; font-size: 0.92rem; }
  </style>
</head>
<body>
  <h1>Blink Bridge Auth Helper</h1>
  <div class="row">Status: <strong id="status">loading...</strong></div>
  <div class="row muted" id="meta"></div>

  <h3>Set Credentials (optional)</h3>
  <div class="row"><input id="username" placeholder="Blink username/email" /></div>
  <div class="row"><input id="password" placeholder="Blink password" type="password" /></div>
  <div class="row"><button id="saveLogin">Save Login</button></div>

  <h3>Submit 2FA Code</h3>
  <div class="row"><input id="code" placeholder="123456" /></div>
  <div class="row"><button id="submit2fa">Verify 2FA</button></div>

  <div class="row"><button id="refresh">Refresh status</button></div>
  <pre id="out" class="muted"></pre>

<script>
const out = document.getElementById('out');
const statusEl = document.getElementById('status');
const metaEl = document.getElementById('meta');

function tokenHeader() {
  const t = localStorage.getItem('bridgeToken') || '';
  return t ? { 'X-Bridge-Token': t } : {};
}

async function loadStatus() {
  const r = await fetch('/auth/status');
  const j = await r.json();
  const txt = j.authenticated ? 'authenticated' : (j.needs2fa ? 'needs 2FA' : 'not authenticated');
  statusEl.textContent = txt;
  statusEl.className = j.authenticated ? 'ok' : 'bad';
  metaEl.textContent = 'authFile: ' + (j.authFile || 'n/a') + ' | hasCredentials: ' + Boolean(j.hasCredentials);
  if (j.lastError) out.textContent = 'lastError: ' + j.lastError;
}

document.getElementById('refresh').onclick = loadStatus;

document.getElementById('saveLogin').onclick = async () => {
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value.trim();
  const r = await fetch('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...tokenHeader() },
    body: JSON.stringify({ username, password })
  });
  out.textContent = JSON.stringify(await r.json(), null, 2);
  await loadStatus();
};

document.getElementById('submit2fa').onclick = async () => {
  const code = document.getElementById('code').value.trim();
  const r = await fetch('/auth/2fa', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...tokenHeader() },
    body: JSON.stringify({ code })
  });
  out.textContent = JSON.stringify(await r.json(), null, 2);
  await loadStatus();
};

(() => {
  const existing = localStorage.getItem('bridgeToken');
  if (!existing) {
    const t = prompt('Optional: BRIDGE_AUTH_TOKEN (leave empty if not configured)');
    if (t) localStorage.setItem('bridgeToken', t.trim());
  }
  loadStatus();
})();
</script>
</body>
</html>`);
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
    console.log(`running BLINK_FETCH_COMMAND every ${cfg.blinkPollIntervalSec}s`);
  }
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
