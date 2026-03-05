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

# seed events store
echo '[]' > config/blink-events.json

docker compose up -d
```

- BirdNET-Go UI: `http://localhost:${BIRDNET_GO_PORT:-8080}`
- Bridge health: `http://localhost:${BRIDGE_PORT:-8787}/health`

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

### Optional external fetch/poller mode

| Variable | Default | Description |
|---|---|---|
| `BLINK_POLL_INTERVAL_SEC` | `180` | Poll interval for `BLINK_FETCH_COMMAND` loop |
| `BRIDGE_URL` | `http://127.0.0.1:8787/bridge/blink/event` | Target bridge endpoint used by `src/blinkPoller.js` |
| `BLINK_FETCH_COMMAND` | _(empty)_ | Command that prints a JSON array of events |

### Compose host mapping / BirdNET-Go companion

| Variable | Default | Description |
|---|---|---|
| `BRIDGE_PORT` | `8787` | Host port mapped to bridge container 8787 |
| `BIRDNET_GO_PORT` | `8080` | Host port mapped to BirdNET-Go UI |
| `TZ` | `America/Chicago` | Timezone passed to BirdNET-Go |

## Event Ingest Methods

You can feed events in three ways:

1. **File polling**: write events to `BLINK_EVENTS_FILE`
2. **HTTP push**: POST one event to `/bridge/blink/event`
3. **External poller**: run `npm run poller` with `BLINK_FETCH_COMMAND`

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
| `/bridge/blink/event` | `POST` | Appends + processes one Blink event |

## Unraid

- Install BirdNET-Go separately (`ghcr.io/tphakala/birdnet-go`).
- Use included `unraid-template.xml` for `blink-bridge`.
- Mount these paths:
  - `/app/config`
  - `/app/work`
  - `/app/output`

**Important:** `/app/output` must map to the same host directory BirdNET-Go is watching.

## Development

```bash
npm install
cp .env.example .env
npm run dev     # bridge server (watch mode)
npm run poller  # optional standalone fetch->push loop
```

## License

MIT
