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

### First-time Blink authentication (lockout-safe)

Blink credentials + auth state are persisted in SQLite (`BLINK_DB_FILE`, default `/app/config/blink-bridge.db`) in the mounted config volume.
Session artifacts are still stored in `BLINK_AUTH_FILE`.

1. Open `http://localhost:${BRIDGE_PORT:-8787}/auth`
2. Click **Save credentials**
3. Click **Test auth now**
4. If prompted, submit code via **Submit 2FA**
5. Confirm status shows `authenticated`
6. Click **Resume fetching** (resume is allowed only when authenticated)

If auth fails, bridge enters a paused/locked state and **will not keep retrying Blink automatically**.
You must explicitly reauthenticate via UI/API (`save credentials`, `test auth`, optional `submit 2FA`) before resuming fetch.

### Migration note (from env-based auth)

Older versions relied on `BLINK_USERNAME`, `BLINK_PASSWORD`, and `BLINK_2FA_CODE` env-driven login attempts.
This has been removed to prevent repeated failed retries/lockouts.

- Existing auth/session files are still used (`BLINK_AUTH_FILE`).
- Configure `BLINK_DB_FILE` and use `/auth` UI/API for credentials + reauth actions.
- `BLINK_2FA_CODE` env flow is deprecated/disabled.

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
| `BRIDGE_AUTH_TOKEN` | _(empty)_ | Optional token required for `POST /auth/login` + `POST /auth/2fa` via `X-Bridge-Token` |
| `BRIDGE_URL` | `http://127.0.0.1:8787/bridge/blink/event` | Used by standalone `src/blinkPoller.js` mode |

### Compose host mapping / BirdNET-Go companion

| Variable | Default | Description |
|---|---|---|
| `BRIDGE_PORT` | `8787` | Host port mapped to bridge container 8787 |
| `BIRDNET_GO_PORT` | `8080` | Host port mapped to BirdNET-Go UI |
| `TZ` | `America/Chicago` | Timezone passed to BirdNET-Go |

## Event Ingest Methods

You can feed events in three ways:

1. **Built-in Blink cloud fetch** (recommended): set Blink auth env vars and keep `BLINK_FETCH_COMMAND=python3 /app/bin/blink_fetch.py`
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
| `/health` | `GET` | Returns status and active config summary |
| `/auth` | `GET` | Minimal auth helper UI for Blink login + 2FA |
| `/auth/status` | `GET` | Returns lockout-safe auth state (`authenticated`, `needs_credentials`, `needs_2fa`, `locked_error`, `paused_fetch`, `last_error`, `last_attempt_at`, `next_allowed_attempt_at`) |
| `/auth/save-credentials` | `POST` | Saves Blink credentials to SQLite (`{username,password}`) and pauses fetch until explicit auth test |
| `/auth/login` | `POST` | Backward-compatible alias for `/auth/save-credentials` |
| `/auth/2fa` | `POST` | Submits MFA code (`{code}`) |
| `/auth/test` | `POST` | Explicitly test auth now |
| `/auth/resume-fetch` | `POST` | Resume fetch loop (only succeeds when authenticated) |
| `/auth/pause-fetch` | `POST` | Pause fetch loop |
| `/bridge/blink/event` | `POST` | Appends + processes one Blink event |

If `BRIDGE_AUTH_TOKEN` is set, include header `X-Bridge-Token` on all `POST /auth/*` calls.

### Auth helper API examples

```bash
# status
curl -s http://localhost:8787/auth/status | jq .

# save credentials
curl -s -X POST http://localhost:8787/auth/save-credentials \
  -H 'Content-Type: application/json' \
  -H 'X-Bridge-Token: YOUR_TOKEN_IF_SET' \
  -d '{"username":"you@example.com","password":"your_password"}' | jq .

# test auth now
curl -s -X POST http://localhost:8787/auth/test \
  -H 'X-Bridge-Token: YOUR_TOKEN_IF_SET' | jq .

# submit 2FA code (if needed)
curl -s -X POST http://localhost:8787/auth/2fa \
  -H 'Content-Type: application/json' \
  -H 'X-Bridge-Token: YOUR_TOKEN_IF_SET' \
  -d '{"code":"123456"}' | jq .

# resume/pause fetch
curl -s -X POST http://localhost:8787/auth/resume-fetch -H 'X-Bridge-Token: YOUR_TOKEN_IF_SET' | jq .
curl -s -X POST http://localhost:8787/auth/pause-fetch -H 'X-Bridge-Token: YOUR_TOKEN_IF_SET' | jq .
```

## Unraid

Use `docker-compose.unraid.yml` to run everything together (UI + worker + bridge):

1. Copy `unraid.env.example` to `.env` and set host share paths.
2. Start stack:

```bash
docker compose -f docker-compose.unraid.yml up -d --build
```

This stack runs:

- `birdnet-go-ui` (dashboard on `BIRDNET_GO_PORT`)
- `birdnet-go-worker` (`birdnet-go directory /blink-processed --watch --recursive --output /birdnet-output`)
- `blink-bridge` (Blink event + WAV extraction + auth helper UI/API)

Useful URLs after startup:

- Bridge health: `http://<unraid-ip>:${BRIDGE_PORT:-8787}/health`
- Bridge auth helper: `http://<unraid-ip>:${BRIDGE_PORT:-8787}/auth`
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
