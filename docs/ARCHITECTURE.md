# BirdWatch Architecture

This document explains how the repo fits together today, with a bias toward operational reality instead of marketing.

## What this project does

BirdWatch turns Blink motion events into one or both of these downstream products:

1. **BirdNET-Go WAV input** for bird-sound classification
2. **RTSP streams** that present each camera's most recent motion clip followed by a held still frame

The repo contains both the bridge that ingests Blink media and the RTSP publisher that keeps MediaMTX fed.

---

## High-level data flow

```text
Blink cloud / bridge fetch
        │
        ▼
blink_fetch.py emits normalized events
        │
        ▼
blink_service.py
  - dedupe by event id
  - resolve/download MP4
  - optionally persist MP4 for RTSP
  - optionally extract WAV for BirdNET-Go
        │
        ├──► output/*.wav  ──► BirdNET-Go watches this directory
        │
        └──► work/blink-downloads/*.mp4
                     │
                     ▼
             rtsp_publisher.py
               - scans MP4s by camera
               - plays each new clip once
               - holds on last frame + silence
                     │
                     ▼
                  MediaMTX
                     │
                     ▼
                RTSP/HLS/WebRTC readers
```

---

## Main entrypoints

### `bin/blink_service.py`
Primary bridge service.

Responsibilities:
- exposes `GET /health`
- accepts `POST /bridge/blink/event`
- periodically runs `BLINK_FETCH_COMMAND`
- dedupes motion IDs across restarts
- downloads or copies MP4s into a stable work area
- persists MP4s for RTSP/debugging when enabled
- extracts mono 48 kHz WAVs for BirdNET-Go when enabled

Use this when you are debugging:
- duplicate processing
- missing WAV output
- disappearing local MP4s
- bridge poll / event ingest issues

### `bin/blink_fetch.py`
Fetch helper that normalizes Blink metadata into bridge events.

It is intentionally separate from the web service so the bridge can ingest from:
- periodic polling
- manual POSTs
- alternate future emitters

### `bin/rtsp_publisher.py`
Long-lived RTSP publisher feeder.

Responsibilities:
- scan a watch directory for MP4s
- group clips by camera name
- queue strictly newer clips once
- keep one persistent ffmpeg publisher per camera
- play queued clips once
- when idle, keep the stream alive with:
  - repeated last frame (`rawvideo yuv420p`)
  - silence (`PCM -> AAC`)

Use this when you are debugging:
- repeated clip playback
- frozen RTSP streams
- still-frame behavior
- MediaMTX path behavior
- tail-frame corruption / tearing symptoms

### `bin/start_birdwatch.sh`
Container-oriented startup glue.

This script wires together env defaults, the bridge service, and the RTSP publisher in the expected deployment shape.

---

## Runtime model

### Bridge model

The bridge is **event oriented**.

For each accepted event:
1. admit only if the event id is not already seen or in-flight
2. obtain the MP4 from `localFile` or `mediaUrl`
3. copy it into a stable temp/work path
4. optionally persist an MP4 copy for RTSP
5. optionally generate a WAV for BirdNET-Go
6. mark the event as done only after success

Important behavior:
- failed events are **not** marked as seen
- a local file may be retried from multiple plausible directories
- local files are waited on briefly so partially-written clips do not get copied too early

### RTSP model

The RTSP publisher is **camera state-machine oriented**.

Per camera lifecycle:

```text
IDLE -> PREPARING -> PLAYING -> HOLDING
                   \-> ERROR
```

Key rules:
- on startup, only the **newest existing clip** seeds playback
- later scans queue only clips with a **strictly newer sort key**
- each queued clip is played **once**
- after playback completes, the stream enters **HOLDING** and repeats the extracted still frame until another newer clip arrives

---

## Why the publisher is persistent

The current RTSP design keeps one long-lived ffmpeg process per camera because reconnecting RTSP readers on every clip boundary is ugly and unreliable.

The persistent publisher receives continuous local inputs:
- audio socket: mono 48 kHz PCM
- video socket: rawvideo `yuv420p`

Short-lived feeder processes decode individual clips and write into those sockets.
When no clip is active, Python itself writes:
- zero-valued PCM silence
- the last extracted raw frame at the configured FPS

This is why readers can stay attached across clip changes.

---

## Files and directories that matter

### `work/`
Scratch and operational state.

Typical contents:
- transient downloaded MP4s
- seen-id state files
- per-camera RTSP work artifacts

### `work/blink-downloads/`
Persistent MP4 area used for RTSP publishing and debugging when MP4 persistence is enabled.

### `output/`
BirdNET-Go WAV handoff directory.

### `config/`
Auth and event config/state samples plus runtime config artifacts.

---

## Naming and ordering conventions

Clip ordering should be based on the Blink-derived timestamp embedded in filenames, not filesystem modification time.

Why:
- copies and restores can change mtime
- prune jobs can reorder directory listings implicitly
- queue monotonicity matters if you want to avoid replaying older motion clips

Camera names are also normalized into slugs for persisted filenames so pruning and grouping happen per camera instead of per raw display name variant.

---

## Common failure modes

### 1. Corrupt tail frames
Symptoms:
- ugly still image
- tearing/smearing-looking hold frame
- ffmpeg decode warnings like `corrupt decoded frame`

Likely cause:
- the hold still is currently extracted from near the end of the MP4
- if Blink produced a slightly corrupt tail, the held frame inherits that damage

### 2. Clip vanished before ready
Symptoms:
- `prepare-vanished`
- bridge/file-not-found logs

Likely cause:
- upstream file path was announced before it stabilized
- another process pruned or moved the file while RTSP was preparing it

### 3. "Old clip replaying"
Usually this is not a true loop.
It is more often one of:
- multiple new motion clips that look visually similar
- process restart causing startup seeding behavior again
- an older clip reappearing under a filename/sort-key pattern the publisher treats as newer

---

## Operational debugging checklist

### Bridge issues
Check:
- bridge logs for `failed id=...`
- whether `localFile` exists long enough to copy
- whether `mediaUrl` fallback is available
- whether WAV generation is enabled

### RTSP issues
Check:
- `queued_new_clips`
- `clip-start`
- `clip-complete`
- `prepare-vanished`
- MediaMTX logs for reader vs publisher connection timing

### Client-side RTSP weirdness
If VLC reports invalid setup path or path-not-found during startup, distinguish between:
- reader connecting before publisher is online
- VLC trying an odd RTSP dialect
- actual server-side path availability problems

---

## Suggested future cleanup

If you continue cleaning the repo, the highest-value follow-ups are:

1. add a dedicated `docs/OPERATIONS.md` for log patterns and troubleshooting
2. add a `docs/ENV.md` table instead of relying on README prose
3. split `rtsp_publisher.py` into:
   - discovery/order helpers
   - publisher process helpers
   - camera worker/state machine
4. decide whether hold-frame extraction should stay near EOF or move to a safer frame-selection strategy
