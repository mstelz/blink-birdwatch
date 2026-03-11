# Blink BirdWatch

Blink BirdWatch is a small bridge repo that turns Blink motion clips into downstream artifacts for two different consumers:

- **BirdNET-Go** via mono 48 kHz WAV files
- **MediaMTX / RTSP readers** via a persistent publisher that plays each new clip once and then holds on a still frame

If you only remember one thing about this repo, make it this:

> `blink_service.py` owns event ingest + media preparation, and `rtsp_publisher.py` owns long-lived camera playback behavior.

---

## What lives here

### Main scripts

- `bin/blink_service.py` - bridge service, event dedupe, MP4 download/copy, WAV generation
- `bin/blink_fetch.py` - Blink fetch helper that emits normalized event payloads
- `bin/rtsp_publisher.py` - persistent per-camera RTSP publisher feeder for MediaMTX
- `bin/start_birdwatch.sh` - container startup wrapper
- `bin/blink_cli.py` / `bin/blink_auth.py` - Blink auth and operational CLI helpers

### Important directories

- `config/` - auth files, samples, fetch state, sample event payloads
- `work/` - runtime scratch/state
- `work/blink-downloads/` - persisted MP4s for RTSP/debugging when enabled
- `output/` - WAV output for BirdNET-Go
- `docs/` - repo docs (start with `docs/ARCHITECTURE.md`)

---

## High-level flow

```text
Blink fetch / event source
        │
        ▼
blink_service.py
  - dedupe by motion id
  - obtain MP4 from localFile or mediaUrl
  - optionally persist MP4
  - optionally extract WAV
        │
        ├──► output/*.wav  -> BirdNET-Go watches this directory
        │
        └──► work/blink-downloads/*.mp4
                     │
                     ▼
             rtsp_publisher.py
               - scan clips by camera
               - queue only newer clips
               - play each clip once
               - hold on last frame + silence
                     │
                     ▼
                  MediaMTX
```

More detail: see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Quick start

```bash
git clone https://github.com/mstelz/blink-birdwatch.git
cd blink-birdwatch
cp .env.example .env
mkdir -p config work output birdnet-go/config birdnet-go/data

docker compose up -d --build
```

Useful URLs:
- Bridge health: `http://localhost:${BRIDGE_PORT:-8787}/health`
- BirdNET-Go UI: `http://localhost:${BIRDNET_GO_PORT:-8080}`

---

## Blink auth (one-time)

Auth is file-based and uses Blink's OAuth v2 flow.
You will be prompted for Blink credentials and then a 2FA code.

```bash
docker exec -it blink-bridge blink login
```

This stores credentials/tokens in `BLINK_AUTH_FILE` (default `/app/config/blink-auth.json`).

Check status:

```bash
docker exec -it blink-bridge blink status
```

If auth expires, run `blink login` again.

### Auth troubleshooting

- Add `--debug` for verbose OAuth output:
  ```bash
  docker exec -it blink-bridge blink login --debug
  ```
- If you see `2FA rate limit exceeded`, Blink has temporarily rate-limited your account. Wait and retry later.
- Make sure the Blink account has 2FA enabled.

---

## Runtime modes

### 1. BirdNET-focused mode
Use this when the goal is audio extraction and bird classification.

Typical behavior:
- bridge ingests Blink motion clips
- WAVs are generated into BirdNET-Go's watched directory
- RTSP publishing may be disabled

### 2. RTSP-focused mode
Use this when the goal is a stable camera-like stream for dashboards, viewers, or downstream consumers.

Typical behavior:
- MP4s are persisted
- WAV generation can be disabled
- `rtsp_publisher.py` publishes each new clip once, then holds the last frame until a newer clip arrives

---

## Key environment variables

See `.env.example` for the full set. The most important ones are below.

### Bridge / ingest

- `BIRDNET_GO_INPUT_DIR` - output WAV directory
- `BLINK_FETCH_COMMAND` - command used for periodic fetch
- `BLINK_FETCH_MODE=download` - prefer BlinkPy-native clip downloading
- `BLINK_POLL_INTERVAL_SEC` - fetch interval
- `BLINK_AUTH_FILE` - auth file path
- `BLINK_FETCH_STATE_FILE` - fetch dedupe state
- `SEEN_IDS_FILE` - bridge dedupe state

### MP4 persistence / cleanup

- `PERSIST_MP4=1` - keep processed MP4s for RTSP/debugging
- `PERSIST_MP4_DIR` - where persisted MP4s live
- `PERSIST_EXISTING_LOCAL_MP4` - avoid duplicate copies if the source is already in the persistent dir
- `CLEANUP_MP4=1` - remove the bridge work MP4 after successful processing
- `PRUNE_OLD_MP4=1` - keep only the newest persisted MP4(s) per camera according to current pruning logic

### BirdNET-only toggle

- `GENERATE_WAV=0` - skip WAV extraction for RTSP-only mode

### RTSP publisher

- `ENABLE_RTSP_PUBLISHER=1` - run the RTSP publisher
- `RTSP_CAMERA_REGEX` - camera grouping regex with `(?P<camera>...)`
- `RTSP_VIDEO_FPS=15` - FPS used for clip playback and still holding
- `RTSP_STILL_HOLD_SEC=0` - compatibility knob; current design effectively holds until replaced
- `RTSP_H264_PRESET` / `RTSP_H264_CRF` - persistent publisher encode settings
- `MEDIAMTX_HOST` / `MEDIAMTX_PORT` - MediaMTX target
- `RTSP_TRANSPORT` - tcp or udp for publishing transport

### Important RTSP note

The publisher is intentionally **long-lived per camera**. It does **not** reconnect MediaMTX readers on every clip boundary. Instead:
- clip feeders come and go
- the publisher stays alive
- when no clip is active, Python feeds silence + a repeated last frame

That is the key design choice that keeps readers attached.

---

## Endpoints

- `GET /health`
- `POST /bridge/blink/event`

### Manual event push example

```bash
curl -X POST http://localhost:8787/bridge/blink/event \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "motion-123",
    "timestamp": "2026-03-03T05:00:00Z",
    "mediaUrl": "https://example/clip.mp4"
  }'
```

---

## Sanity checks

```bash
python3 -m py_compile bin/blink_service.py bin/blink_auth.py bin/blink_fetch.py bin/blink_cli.py bin/rtsp_publisher.py
docker compose config
```

If you are changing RTSP behavior, also run the targeted test file:

```bash
pytest tests/test_rtsp_publisher.py
```

---

## Operational behavior worth knowing

### How clip playback works today

For each camera:
1. existing files at startup seed only the **newest** clip
2. newly discovered clips with a strictly newer sort key are queued once
3. each queued clip is played once
4. after playback completes, the stream enters **holding** mode
5. holding mode sends:
   - the extracted last frame as repeated rawvideo
   - silence as audio

### Why RTSP sometimes looks repetitive

If the same scene seems to appear every few minutes, that often means:
- Blink is producing multiple separate motion clips with similar content
- not necessarily that one old clip is looping forever

### Why still frames can look ugly

Hold mode currently extracts a frame near the clip tail. If the source MP4 has a corrupted tail frame, the held still can inherit that damage.

---

## Common troubleshooting clues

### MediaMTX says `no stream is available on path`
Usually the reader connected before the publisher had come online for that path.

### VLC says `invalid SETUP path`
Often VLC retry/dialect noise rather than the root cause of a publisher bug.

### RTSP logs show `prepare-vanished`
The publisher discovered a clip file, but it disappeared before preparation completed.
That usually points to unstable local file lifecycle rather than MediaMTX itself.

### ffmpeg shows `corrupt decoded frame`
That is a real source/decode issue and can affect clip playback or held still quality.

---

## Unraid

Use `docker-compose.unraid.yml` together with `unraid.env.example`.

```bash
cp unraid.env.example .env
docker compose -f docker-compose.unraid.yml up -d --build
```

---

## Repo cleanup direction

This repo has grown from a single-purpose bridge into a bridge + RTSP publishing system. If you keep refactoring it, the best next structural improvements are:

- split RTSP publisher helpers into smaller modules
- add a dedicated environment-variable reference doc
- add a troubleshooting/operations doc with common log patterns
- tighten tests around clip discovery, hold-frame generation, and file-stability races
