# Blink BirdWatch

Blink BirdWatch is a small bridge service that turns Blink motion clips into BirdNET-Go input audio.

It runs alongside [BirdNET-Go](https://github.com/tphakala/birdnet-go):

1. receives/loads Blink motion events
2. downloads the MP4 clip
3. extracts mono 48kHz WAV via `ffmpeg`
4. drops WAV into a shared `/output` directory watched by BirdNET-Go

Built for Unraid, but works anywhere Docker Compose works.

## Architecture

```text
Blink motion clip event
        │
        ▼
blink-bridge (this repo)
  ├─ download clip (mediaUrl)
  ├─ ffmpeg extract audio (.wav)
  └─ write wav -> shared /output volume
                         │
                         ▼
birdnet-go container
  ├─ classify species
  ├─ write DB/results
  └─ serve web UI (:8080)
```

## Containers

| Container | Image | Purpose |
|---|---|---|
| `blink-bridge` | `ghcr.io/mstelz/blink-birdwatch:latest` | Ingest Blink events + produce WAVs |
| `birdnet-go` | `ghcr.io/tphakala/birdnet-go:latest` | Analysis, dashboard, tracking/database |

## Quick Start

```bash
git clone https://github.com/mstelz/blink-birdwatch.git
cd blink-birdwatch
cp .env.example .env

# host paths mounted by docker-compose
mkdir -p config work output birdnet-go/config birdnet-go/data

# optional file ingest seed (safe to leave empty)
echo '[]' > config/blink-events.json

docker compose up -d
```

- BirdNET-Go UI: `http://localhost:${BIRDNET_GO_PORT:-8080}`
- Bridge health: `http://localhost:${BRIDGE_PORT:-8787}/health`

## Blink Login (recommended)

Blink credentials + auth state are persisted in SQLite (`BLINK_DB_FILE`, default `/app/config/blink-bridge.db`) in the mounted config volume.
Session artifacts are still stored in `BLINK_AUTH_FILE`.

Run this in the container:

```bash
docker exec -it blink-bridge blink login
```

The helper will:
1. Prompt for username + password
2. Handle 2FA in the same process (when required)
3. Save session auth
4. Resume fetch automatically on success

Useful helper commands:

```bash
docker exec -it blink-bridge blink status
docker exec -it blink-bridge blink test
docker exec -it blink-bridge blink pause
docker exec -it blink-bridge blink resume
```

If auth fails, bridge enters a paused/locked state and **will not keep retrying Blink automatically**.
You must explicitly reauthenticate (`blink login`) before resuming fetch.

### Day-1 sanity check (copy/paste)

```bash
docker compose ps
docker exec -it blink-bridge blink login
docker exec -it blink-bridge blink status
docker logs -f blink-bridge
```

## Configuration

All settings are env vars (see `.env.example`).

### Core bridge

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8787` | Bridge listen port inside container |
| `POLL_INTERVAL_SEC` | `180` | Poll frequency for `BLINK_EVENTS_FILE` |
| `BLINK_EVENTS_FILE` | `./config/blink-events.json` | JSON file with queued events |
| `WORK_DIR` | `./work` | Temporary MP4/WAV processing directory |
| `BIRDNET_GO_INPUT_DIR` | `/app/output` | Directory where WAVs are dropped |

### Blink cloud fetch mode (built-in auth)

| Variable | Default | Description |
|---|---|---|
| `BLINK_POLL_INTERVAL_SEC` | `180` | Poll interval for running `BLINK_FETCH_COMMAND` inside `blink-bridge` |
| `BLINK_FETCH_COMMAND` | `python3 /app/bin/blink_fetch.py` | Command that prints a JSON array of events |
| `BLINK_DB_FILE` | `/app/config/blink-bridge.db` | SQLite store for credentials + auth state machine |
| `BLINK_AUTH_FILE` | `/app/config/blink-auth.json` | Persisted Blink auth/session file |
| `BLINK_FETCH_STATE_FILE` | `/app/config/blink-fetch-state.json` | Dedupe state for emitted events |
| `BLINK_CAMERA_NAMES` | _(empty)_ | Optional comma-separated camera names to include |
| `BLINK_FETCH_MAX_EVENTS` | `25` | Max events emitted per fetch run |
| `BRIDGE_URL` | `http://127.0.0.1:8787/bridge/blink/event` | Used by standalone `src/blinkPoller.js` mode |

### Compose host mapping / BirdNET-Go companion

| Variable | Default | Description |
|---|---|---|
| `BRIDGE_PORT` | `8787` | Host port mapped to bridge container 8787 |
| `BIRDNET_GO_PORT` | `8080` | Host port mapped to BirdNET-Go UI |
| `TZ` | `America/Chicago` | Timezone passed to BirdNET-Go |

## Event Ingest Methods

You can feed events in three ways:

1. **Built-in Blink cloud fetch** (recommended): run `blink login` once, then keep `BLINK_FETCH_COMMAND=python3 /app/bin/blink_fetch.py`
2. **File polling**: write events to `BLINK_EVENTS_FILE`
3. **HTTP push**: POST one event to `/bridge/blink/event`
4. **Standalone poller**: run `npm run poller` with any custom `BLINK_FETCH_COMMAND`

### Event schema

```json
{
  "id": "unique-motion-id",
  "timestamp": "2026-03-03T03:00:00Z",
  "mediaUrl": "https://example/clip.mp4",
  "thumbnailUrl": "https://example/thumb.jpg",
  "source": "blink"
}
```

- `id` is required and used for dedupe.
- `mediaUrl` is required for clip download/extraction.

### HTTP push example

```bash
curl -X POST http://localhost:8787/bridge/blink/event \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "motion-123",
    "timestamp": "2026-03-03T05:00:00Z",
    "mediaUrl": "http://your-blink-server/clip.mp4"
  }'
```

## API

| Endpoint | Method | Description |
|---|---|---|
| `/health` | `GET` | Returns status and active config summary (includes auth state) |
| `/auth/status` | `GET` | Returns lockout-safe auth state (`authenticated`, `needs_credentials`, `needs_2fa`, `locked_error`, `paused_fetch`, `last_error`, `last_attempt_at`, `next_allowed_attempt_at`) |
| `/bridge/blink/event` | `POST` | Appends + processes one Blink event |

## Unraid

Use `docker-compose.unraid.yml` to run everything together (UI + worker + bridge):

1. Copy `unraid.env.example` to `.env` and set host share paths.
2. Start stack:

```bash
docker compose -f docker-compose.unraid.yml pull
docker compose -f docker-compose.unraid.yml up -d
```

This stack runs:

- `birdnet-go-ui` (dashboard on `BIRDNET_GO_PORT`)
- `birdnet-go-worker` (`birdnet-go directory /blink-processed --watch --recursive --output /birdnet-output`)
- `blink-bridge` (Blink event + WAV extraction + lockout-safe auth)

Useful URLs after startup:

- Bridge health: `http://<unraid-ip>:${BRIDGE_PORT:-8787}/health`
- BirdNET UI: `http://<unraid-ip>:${BIRDNET_GO_PORT:-8080}`

Check status/logs:

```bash
docker compose -f docker-compose.unraid.yml ps
docker compose -f docker-compose.unraid.yml logs -f birdnet-go-ui
docker compose -f docker-compose.unraid.yml logs -f birdnet-go-worker
docker compose -f docker-compose.unraid.yml logs -f blink-bridge
```

## Development

```bash
npm install
cp .env.example .env
npm run dev     # bridge server (watch mode)
npm run poller  # optional standalone fetch->push loop
```

## License

MIT
