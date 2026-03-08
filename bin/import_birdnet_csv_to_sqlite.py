#!/usr/bin/env python3
"""Import BirdNET-Go CSV output into BirdNET-Go SQLite DB.

Why:
- BirdNET-Go `directory --type csv` writes CSV files but does NOT write detections
  into the SQLite database that the BirdNET-Go UI reads.
- This script bridges that gap by inserting rows into `notes` + `results`.

CSV format expected (header):
  Start (s),End (s),Scientific name,Common name,Confidence

It is intentionally conservative:
- Skips rows missing required fields.
- Avoids duplicates by checking existing notes for the same clip/begin/end/species/confidence.

Usage:
  python3 bin/import_birdnet_csv_to_sqlite.py \
    --db birdnet-go/data/birdnet.db \
    --csv-dir birdnet-output \
    --clip-dir .local/output \
    --tz America/Chicago

Tip:
- Run it repeatedly (or via cron) to continuously ingest newly produced CSVs.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


FILENAME_RE = re.compile(
    r"^blink_(?P<date>\d{4}-\d{2}-\d{2})T(?P<h>\d{2})-(?P<m>\d{2})-(?P<s>\d{2})-(?P<us>\d{6})(?P<tzsign>[+-])(?P<tzh>\d{2})-(?P<tzm>\d{2})$"
)


@dataclass
class DetectionRow:
    start_s: float
    end_s: float
    scientific_name: str
    common_name: str
    confidence: float


def parse_clip_timestamp(clip_stem: str) -> datetime:
    """Parse blink_ timestamped stems produced by blink_service.py.

    Example stem:
      blink_2026-03-08T12-50-10-804059+00-00

    Returns an aware datetime.
    """
    if clip_stem.endswith(".wav"):
        clip_stem = clip_stem[:-4]
    if clip_stem.startswith("blink_"):
        ts = clip_stem[len("blink_") :]
    else:
        ts = clip_stem

    m = FILENAME_RE.match(ts)
    if not m:
        raise ValueError(f"Unrecognized clip name format: {clip_stem}")

    tzsign = 1 if m.group("tzsign") == "+" else -1
    tzh = int(m.group("tzh"))
    tzm = int(m.group("tzm"))
    offset = timezone(tzsign * timedelta(hours=tzh, minutes=tzm))

    dt = datetime(
        int(m.group("date")[0:4]),
        int(m.group("date")[5:7]),
        int(m.group("date")[8:10]),
        int(m.group("h")),
        int(m.group("m")),
        int(m.group("s")),
        int(m.group("us")),
        tzinfo=offset,
    )
    return dt


def _parse_start_end(value: str) -> float:
    """Parse BirdNET-Go CSV start/end fields.

    We have seen two formats:
    - seconds as float: "0.0" / "1.5"
    - a sentinel datetime: "0001-01-01 00:00:00" (time portion is what matters)

    Returns seconds.
    """
    v = (value or "").strip()
    if not v:
        raise ValueError("empty")

    # Float seconds
    try:
        return float(v)
    except Exception:
        pass

    # Sentinel datetime or time
    # Examples: "0001-01-01 00:00:01" or "00:00:01"
    if " " in v:
        v = v.split(" ", 1)[1].strip()

    parts = v.split(":")
    if len(parts) != 3:
        raise ValueError(f"unrecognized time format: {value}")
    h, m, s = parts
    return int(h) * 3600 + int(m) * 60 + float(s)


def read_csv(path: Path) -> list[DetectionRow]:
    rows: list[DetectionRow] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                start_s = _parse_start_end(r.get("Start (s)") or "")
                end_s = _parse_start_end(r.get("End (s)") or "")
                scientific = (r.get("Scientific name") or "").strip()
                common = (r.get("Common name") or "").strip()
                conf = float((r.get("Confidence") or "").strip())
            except Exception:
                continue

            if not scientific and not common:
                continue
            if end_s <= start_s:
                continue

            rows.append(
                DetectionRow(
                    start_s=start_s,
                    end_s=end_s,
                    scientific_name=scientific,
                    common_name=common,
                    confidence=conf,
                )
            )
    return rows


def ensure_schema(conn: sqlite3.Connection) -> None:
    # Quick sanity check: required tables/cols exist.
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('notes','results')")
    found = {r[0] for r in cur.fetchall()}
    if found != {"notes", "results"}:
        raise RuntimeError(f"DB missing required tables. Found: {sorted(found)}")


def note_exists(
    conn: sqlite3.Connection,
    clip_name: str,
    begin_time_iso: str,
    end_time_iso: str,
    scientific_name: str,
    confidence: float,
) -> int | None:
    cur = conn.execute(
        """
        SELECT id FROM notes
        WHERE clip_name = ?
          AND begin_time = ?
          AND end_time = ?
          AND scientific_name = ?
          AND confidence = ?
        LIMIT 1
        """,
        (clip_name, begin_time_iso, end_time_iso, scientific_name, confidence),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def insert_detection(
    conn: sqlite3.Connection,
    *,
    tz: ZoneInfo,
    clip_name: str,
    clip_dt: datetime,
    det: DetectionRow,
    source_node: str = "blink-bridge",
    threshold: float | None = None,
    sensitivity: float | None = None,
) -> bool:
    begin_dt = clip_dt + timedelta(seconds=det.start_s)
    end_dt = clip_dt + timedelta(seconds=det.end_s)

    # Store date/time fields in local tz for UI friendliness.
    begin_local = begin_dt.astimezone(tz)

    date_txt = begin_local.strftime("%Y-%m-%d")
    time_txt = begin_local.strftime("%H:%M:%S")

    begin_iso = begin_dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    end_iso = end_dt.astimezone(timezone.utc).isoformat(timespec="seconds")

    sci = det.scientific_name or None
    com = det.common_name or None

    existing = note_exists(conn, clip_name, begin_iso, end_iso, sci or "", float(det.confidence)) if sci else None
    if existing:
        return False

    cur = conn.execute(
        """
        INSERT INTO notes (
          source_node, date, time, begin_time, end_time,
          species_code, scientific_name, common_name, confidence,
          latitude, longitude, threshold, sensitivity,
          clip_name, processing_time
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            source_node,
            date_txt,
            time_txt,
            begin_iso,
            end_iso,
            None,
            sci,
            com,
            float(det.confidence),
            None,
            None,
            threshold,
            sensitivity,
            clip_name,
            None,
        ),
    )
    note_id = cur.lastrowid

    # `results.species` seems to track the scientific name in current schema.
    conn.execute(
        "INSERT INTO results (note_id, species, confidence) VALUES (?,?,?)",
        (note_id, sci or com or "unknown", float(det.confidence)),
    )
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--csv-dir", required=True, type=Path)
    ap.add_argument("--clip-dir", required=False, type=Path, help="Optional dir containing .wav clips (not strictly needed)")
    ap.add_argument("--tz", default="America/Chicago")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true", help="Process current CSVs then exit (default behavior).")
    args = ap.parse_args()

    tz = ZoneInfo(args.tz)

    db_path = args.db
    csv_dir = args.csv_dir
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")
    if not csv_dir.exists():
        raise SystemExit(f"CSV dir not found: {csv_dir}")

    csv_files = sorted(csv_dir.glob("*.wav.csv"))

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        ensure_schema(conn)

        inserted = 0
        skipped = 0
        for csv_path in csv_files:
            clip_name = csv_path.name[: -len(".csv")]
            clip_stem = clip_name
            try:
                clip_dt = parse_clip_timestamp(clip_stem)
            except Exception:
                # Fallback: use file mtime (UTC)
                clip_dt = datetime.fromtimestamp(csv_path.stat().st_mtime, tz=timezone.utc)

            dets = read_csv(csv_path)
            if not dets:
                continue

            for det in dets:
                if args.dry_run:
                    inserted += 1
                    continue
                ok = insert_detection(conn, tz=tz, clip_name=clip_name, clip_dt=clip_dt, det=det)
                if ok:
                    inserted += 1
                else:
                    skipped += 1

        if not args.dry_run:
            conn.commit()

        print(f"import complete csv_files={len(csv_files)} inserted={inserted} skipped={skipped}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
