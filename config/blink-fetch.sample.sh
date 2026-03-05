#!/usr/bin/env bash
# Sample adapter command used by BLINK_FETCH_COMMAND.
# Replace this with your real Blink cloud pull logic.

# Must output a JSON array like:
cat <<JSON
[
  {
    "id": "motion-$(date +%s)",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "mediaUrl": "https://example.local/blink/latest.mp4",
    "thumbnailUrl": "https://example.local/blink/latest.jpg"
  }
]
JSON
