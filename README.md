# Blink BirdWatch (Python)

Blink BirdWatch is a Python bridge that turns Blink motion clips into BirdNET-Go input audio.

Flow:
1. poll Blink on a timer (`BLINK_FETCH_COMMAND`)
2. dedupe by motion ID (persisted to disk)
3. download MP4 (or use local file)
4. convert to mono 48k WAV with `ffmpeg`
5. drop WAV in BirdNET-Go watched directory

## Endpoints

- `GET /health`
- `POST /bridge/blink/event`

## Quick start

```bash
git clone https://github.com/mstelz/blink-birdwatch.git
cd blink-birdwatch
cp .env.example .env
mkdir -p config work output birdnet-go/config birdnet-go/data

docker compose up -d --build
```

- Bridge health: `http://localhost:${BRIDGE_PORT:-8787}/health`
- BirdNET-Go UI: `http://localhost:${BIRDNET_GO_PORT:-8080}`

## Blink auth (one-time)

Auth is file-only (no SQLite). Uses Blink's OAuth v2 flow — you will be prompted for your Blink username and password, then a 2FA code sent to your email or phone.

```bash
docker exec -it blink-bridge blink login
```

This stores credentials/tokens in `BLINK_AUTH_FILE` (default `/app/config/blink-auth.json`).

Check status:

```bash
docker exec -it blink-bridge blink status
```

If auth expires, rerun `blink login`.

**Troubleshooting:**

- Add `--debug` to see verbose OAuth flow output:
  ```bash
  docker exec -it blink-bridge blink login --debug
  ```
- If you see `2FA rate limit exceeded`, Blink has temporarily blocked your account due to too many login attempts. Wait 24 hours and try again.
- Ensure your Blink account has 2FA enabled — the OAuth flow requires it.

## Env

See `.env.example`.

Key vars:

- `BIRDNET_GO_INPUT_DIR` output WAV directory
- `BLINK_FETCH_COMMAND` fetch command (default `python3 /app/bin/blink_fetch.py`)
- `BLINK_FETCH_MODE=download` to prefer BlinkPy's native clip downloader
- `BLINK_POLL_INTERVAL_SEC` poll interval
- `BLINK_AUTH_FILE` auth file path
- `BLINK_FETCH_STATE_FILE` fetch dedupe state
- `SEEN_IDS_FILE` bridge dedupe state
- `PERSIST_MP4=1` to keep processed MP4s for RTSP publishing / debugging
- `GENERATE_WAV=0` for RTSP-only mode (skip BirdNET WAV extraction)
- For integrated RTSP-in-birdwatch deployments, the startup script currently forces a known-good default camera regex (`^(?P<camera>.+)-\d{4}-.*\.mp4$`) to avoid startup env mangling seen in some container launches
- `RTSP_STILL_HOLD_SEC=0` means "hold the final frame effectively forever until a newer clip replaces it"
- `RTSP_VIDEO_FPS=15` controls the long-lived publisher's clip/still frame rate
- `RTSP_H264_PRESET` and `RTSP_H264_CRF` tune output quality vs CPU usage for the persistent publisher
- `RTSP_MJPEG_Q` is now a legacy compatibility env var and is ignored; the persistent transport no longer uses MJPEG internally
- RTSP publishing now keeps one long-lived ffmpeg RTSP publisher per camera; clip changes only swap the short-lived clip feeders feeding that publisher, so MediaMTX readers stay connected when a clip ends or a newer clip arrives
- Each camera now has an explicit queue/state machine: existing files at startup seed only the newest clip, each newly discovered newer clip is queued once, played once, and then the stream enters still+silence hold until another newer clip arrives
- Hold mode audio is zero-valued PCM silence before AAC encoding, so the RTSP hold state is intentionally silent rather than replaying trailing clip audio
- The publisher no longer depends on handing multiple MPEG-TS producers across one pipe, and it no longer uses a lossy MJPEG intermediate; it now ingests rawvideo yuv420p + PCM audio continuously, which avoids MPEG-TS DTS/PPS boundary corruption while materially reducing unnecessary re-encode loss inside the persistent pipeline
- Tradeoff: because the persistent rawvideo publisher must keep a stable geometry, the first successful clip for a camera locks that camera's canvas size for the process lifetime; later clips with different dimensions are scaled/padded into that same canvas instead of restarting the publisher and dropping RTSP readers
- Clip ordering is based on timestamps parsed from Blink filenames (not filesystem mtime), which avoids bouncing backward to older clips after prune/copy operations
- The bridge now waits briefly for local MP4 paths to stabilize, retries alternate local candidates, and falls back to `mediaUrl` when available if a `localFile` disappears mid-handoff
- On Unraid, RTSP publishing runs inside `birdwatch` (set `ENABLE_RTSP_PUBLISHER=1`) and publishes to MediaMTX
- `BLINK_FETCH_IGNORE_SEEN=1` and `BLINK_FETCH_NO_SAVE_STATE=1` for one-shot replay testing of recent clips

## Manual event push example

```bash
curl -X POST http://localhost:8787/bridge/blink/event \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "motion-123",
    "timestamp": "2026-03-03T05:00:00Z",
    "mediaUrl": "https://example/clip.mp4"
  }'
```

## Sanity checks

```bash
python3 -m py_compile bin/blink_service.py bin/blink_auth.py bin/blink_fetch.py bin/blink_cli.py
docker compose config
```

## Unraid

Use `docker-compose.unraid.yml` with `unraid.env.example`.

```bash
cp unraid.env.example .env
docker compose -f docker-compose.unraid.yml up -d --build
```
