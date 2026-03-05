import fs from 'node:fs';
import path from 'node:path';

/**
 * Reads motion events from a local JSON file you can populate from Blink cloud pulls.
 * File format: [{id, timestamp, mediaUrl, thumbnailUrl, source:'blink'}]
 */
export function getBlinkEvents(filePath) {
  const absolute = path.resolve(process.cwd(), filePath);
  if (!fs.existsSync(absolute)) return [];
  const raw = fs.readFileSync(absolute, 'utf8');
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}
