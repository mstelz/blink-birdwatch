import { execFile as execFileCb } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs';
import path from 'node:path';
import { pipeline } from 'node:stream/promises';

const execFile = promisify(execFileCb);

export async function downloadFile(url, outPath) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`download failed: ${res.status}`);
  if (!res.body) throw new Error('download failed: empty response body');

  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  const fileStream = fs.createWriteStream(outPath);

  try {
    await pipeline(res.body, fileStream);
  } catch (err) {
    try {
      if (fs.existsSync(outPath)) fs.unlinkSync(outPath);
    } catch {
      // best effort cleanup
    }
    throw err;
  }

  return outPath;
}

export async function extractAudioFromVideo(videoPath, audioPath) {
  await execFile('ffmpeg', ['-y', '-i', videoPath, '-vn', '-ac', '1', '-ar', '48000', audioPath]);
  return audioPath;
}
