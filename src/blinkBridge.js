import fs from 'node:fs';
import path from 'node:path';

function ensureFile(filePath) {
  const abs = path.resolve(process.cwd(), filePath);
  fs.mkdirSync(path.dirname(abs), { recursive: true });
  if (!fs.existsSync(abs)) fs.writeFileSync(abs, '[]\n', 'utf8');
  return abs;
}

export function appendBlinkEvent(filePath, event) {
  if (!event?.id || typeof event.id !== 'string') return false;

  const abs = ensureFile(filePath);
  let list = [];
  try {
    list = JSON.parse(fs.readFileSync(abs, 'utf8'));
    if (!Array.isArray(list)) list = [];
  } catch {
    list = [];
  }

  if (list.some((e) => e.id === event.id)) return false;

  list.push({
    id: event.id,
    timestamp: event.timestamp || new Date().toISOString(),
    mediaUrl: event.mediaUrl || null,
    localFile: event.localFile || null,
    thumbnailUrl: event.thumbnailUrl || null,
    source: event.source || 'blink'
  });

  fs.writeFileSync(abs, JSON.stringify(list, null, 2) + '\n', 'utf8');
  return true;
}
