#!/usr/bin/env python3
"""24‑hour TV channel builder.

This script assembles a 24‑hour block of programming from a local
library of full‑length episodes and short bumper/filler reels.  It
normalizes each file to a common format via ffmpeg, concatenates them
into an HTTP Live Streaming (HLS) playlist and writes ancillary
metadata such as an XMLTV programme guide and a `version.json` manifest.

The scheduler tracks recently used episodes to minimise repeats and
supports optional staging/publishing logic to build tomorrow’s block
ahead of time.  Use `--stage-only` to build into the staging directory
without publishing, or `--auto` to let the script decide when to stage
and when to publish based on the age of the current live block.

Adjust the constants below (`WORK_DIR`, `LIVE_DIR`, `STAGE_DIR`,
`SOURCE_DIRS` and `REEL_DIRS`) to match your environment.  Paths are
absolute; the defaults are placeholders and will not work until you
point them at your own media library and web server directories.

Run `python3 build_block.py --help` for a brief usage summary.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Timezone for programme scheduling and timestamps.  Change this to your
# local zone, e.g. "America/Los_Angeles" or "Europe/London".
TZ = ZoneInfo("America/New_York")

# Target programme length and acceptable variance.  The builder will keep
# adding episodes and filler until at least MIN_SECONDS are reached.  If
# content slightly overshoots TARGET_SECONDS it is truncated; if it ends
# before MIN_SECONDS the build aborts.
TARGET_SECONDS = 24 * 60 * 60
MIN_SECONDS = int(23.75 * 60 * 60)
MAX_SECONDS = int(24.25 * 60 * 60)

# Working directories.  These should be adjusted for your environment.
# WORK_DIR holds temporary files and logs; LIVE_DIR and STAGE_DIR are
# published via your web server.  The script will clean TMP_DIR and
# STAGE_DIR at the start of each build.
WORK_DIR = Path("/path/to/builder/work")
TMP_DIR = WORK_DIR / "tmp_parts"
LOG_DIR = WORK_DIR / "logs"

LIVE_DIR = Path("/path/to/www/channel-live")
STAGE_DIR = Path("/path/to/www/channel-stage")

# Directories containing bumper/filler reels.  Reels are short clips
# inserted between episodes to break up the pacing.  You can list
# multiple directories here; all video files under them will be
# considered.
REEL_DIRS = [
    Path("/path/to/reels/toonami"),
    Path("/path/to/reels/adultswim_generated"),
]

# Directories containing full‑length episodes.  The builder recurses
# through these folders and collects files with recognised video
# extensions.  Add as many directories as you like; if there are fewer
# than ~60 episodes available the build will abort.
SOURCE_DIRS = [
    Path("/path/to/media/show1"),
    Path("/path/to/media/show2"),
    # ...
]

# Recognised video extensions.  Add more if needed.
VIDEO_EXTS = {".mp4", ".mkv", ".m4v", ".avi", ".mov"}

# Filenames for plan, concatenation list, XMLTV guide and version manifest.
PLAN_CSV = WORK_DIR / "plan.csv"
CONCAT_PLAN = WORK_DIR / "concat.txt"
XMLTV_OUT = STAGE_DIR / "xmltv.xml"
VERSION_OUT = STAGE_DIR / "version.json"

# Episode history.  `EPISODE_HISTORY_PATH` stores which episodes have
# appeared in recent blocks.  `EPISODE_HISTORY_BLOCKS` controls how
# many blocks to keep in history when avoiding repeats.
EPISODE_HISTORY_PATH = LOG_DIR / "used_episodes_history.json"
EPISODE_HISTORY_BLOCKS = 7

# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------

def run(cmd: list[str]):
    """Execute a subprocess, printing the command to the console.

    All commands run via this wrapper will raise a `CalledProcessError`
    if they exit with a non‑zero status.
    """
    print()
    print(" ".join(str(x) for x in cmd))
    subprocess.run(cmd, check=True)


def ffprobe_duration(path: Path) -> float:
    """Return the duration of a media file in seconds using ffprobe.

    If ffprobe fails or returns invalid output, the duration defaults
    to 0.0 seconds.
    """
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def collect_videos(paths: list[Path]) -> list[Path]:
    """Collect all video files under a list of directories.

    Non‑existent directories are silently skipped.  Duplicate paths are
    removed and the resulting list is sorted for determinism.
    """
    files: list[Path] = []
    for root in paths:
        if not root.exists():
            print(f"Skipping missing source: {root}")
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in VIDEO_EXTS:
                files.append(path)
    return sorted(set(files))


def label_from_path(path: Path) -> str:
    """Derive a human‑readable label from a file path."""
    text = path.name
    text = text.rsplit(".", 1)[0]
    text = text.replace(".", " ")
    text = text.replace("_", " ")
    text = " ".join(text.split())
    return text[:120]


def xmltv_time(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S %z")


def clean_dir(path: Path) -> None:
    """Remove a directory if it exists and recreate it."""
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def load_episode_history() -> dict:
    """Load the history of used episodes from JSON.

    The history file contains a list of recent blocks with the
    episodes used in each.  If the file is missing or corrupt, an
    empty history is returned.
    """
    if not EPISODE_HISTORY_PATH.exists():
        return {"blocks": []}
    try:
        data = json.loads(EPISODE_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Warning: could not read episode history {EPISODE_HISTORY_PATH}: {exc}")
        return {"blocks": []}
    if not isinstance(data, dict):
        return {"blocks": []}
    blocks = data.get("blocks", [])
    if not isinstance(blocks, list):
        blocks = []
    cleaned_blocks: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        episodes = block.get("episodes", [])
        if not isinstance(episodes, list):
            episodes = []
        cleaned_blocks.append(
            {
                "block_id": str(block.get("block_id", "")),
                "generated_at": str(block.get("generated_at", "")),
                "episodes": [str(item) for item in episodes if item],
            }
        )
    # Keep only the most recent N blocks
    return {"blocks": cleaned_blocks[-EPISODE_HISTORY_BLOCKS:]}


def recently_used_episode_paths(history: dict) -> set[str]:
    """Return a set of file paths that have been used recently."""
    recent: set[str] = set()
    for block in history.get("blocks", []):
        for path in block.get("episodes", []):
            recent.add(path)
    return recent


def save_episode_history(block_id: str, generated_at: str, used_episode_paths: set[str]) -> None:
    """Append a new block to the episode history and write it to disk."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    history = load_episode_history()
    blocks = history.get("blocks", [])
    blocks.append(
        {
            "block_id": block_id,
            "generated_at": generated_at,
            "episodes": sorted(str(path) for path in used_episode_paths),
        }
    )
    history = {
        "keep_last_blocks": EPISODE_HISTORY_BLOCKS,
        "updated_at": datetime.now(TZ).isoformat(),
        "blocks": blocks[-EPISODE_HISTORY_BLOCKS:],
    }
    tmp_path = EPISODE_HISTORY_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
    tmp_path.replace(EPISODE_HISTORY_PATH)


def choose_reels(reels: list[Path], recent_reels: list[Path]) -> list[Path]:
    """Select one or two filler reels, avoiding recent picks.

    A short memory is kept in `recent_reels` to avoid showing the same
    bumpers repeatedly.  If there are fewer available reels than
    requested, the full list is used.
    """
    count = random.choice([1, 2])
    available = [r for r in reels if r not in recent_reels]
    if len(available) < count:
        available = reels[:]
    selected = random.sample(available, min(count, len(available)))
    for reel in selected:
        recent_reels.append(reel)
    # Limit memory of recently used reels; adjust as you like
    while len(recent_reels) > 25:
        recent_reels.pop(0)
    return selected


def normalize_item(input_path: Path, output_path: Path, label: str) -> None:
    """Transcode a single media item into MPEG‑TS with normalized settings."""
    print()
    print(f"Normalizing {label}")
    print(input_path)
    print(f"-> {output_path}")
    run(
        [
            "nice", "-n", "19",
            "ionice", "-c2", "-n7",
            "ffmpeg", "-y",
            "-hide_banner",
            "-nostdin",
            "-i", str(input_path),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-vf", "scale=-2:720,fps=30000/1001,format=yuv420p",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-maxrate", "2500k",
            "-bufsize", "5000k",
            "-g", "60",
            "-keyint_min", "60",
            "-sc_threshold", "0",
            "-c:a", "aac",
            "-b:a", "160k",
            "-ar", "48000",
            "-ac", "2",
            "-af", "aresample=async=1:first_pts=0",
            "-fflags", "+genpts",
            "-avoid_negative_ts", "make_zero",
            "-muxpreload", "0",
            "-muxdelay", "0",
            "-f", "mpegts",
            str(output_path),
        ]
    )


def write_xmltv(rows: list[dict], block_start: datetime) -> None:
    """Write a simple XMLTV file for the planned schedule."""
    programmes: list[str] = []
    current = block_start
    for row in rows:
        start = current
        stop = current + timedelta(seconds=row["duration"])
        current = stop
        title = row["title"]
        desc = row["path"]
        programmes.append(
            f'''  <programme start="{xmltv_time(start)}" stop="{xmltv_time(stop)}" channel="channel-1">\n    <title lang="en">{escape(title)}</title>\n    <desc lang="en">{escape(desc)}</desc>\n  </programme>'''
        )
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="Simple TV Builder">\n  <channel id="channel-1">\n    <display-name>Channel 1</display-name>\n  </channel>\n{chr(10).join(programmes)}\n</tv>\n'''
    XMLTV_OUT.write_text(xml, encoding="utf-8")


def publish_stage() -> None:
    """Atomically replace the live directory with the staged directory."""
    old_dir = LIVE_DIR.with_name(LIVE_DIR.name + "-old")
    if old_dir.exists():
        shutil.rmtree(old_dir)
    if LIVE_DIR.exists():
        LIVE_DIR.rename(old_dir)
    STAGE_DIR.rename(LIVE_DIR)
    print()
    print(f"Published new block to {LIVE_DIR}")
    if old_dir.exists():
        shutil.rmtree(old_dir)


def build_block(publish: bool = True) -> None:
    """Build a 24‑hour block and optionally publish it."""
    random.seed()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    clean_dir(TMP_DIR)
    clean_dir(STAGE_DIR)
    episodes = collect_videos(SOURCE_DIRS)
    reels = collect_videos(REEL_DIRS)
    if len(episodes) < 60:
        print(f"Not enough episodes found: {len(episodes)}")
        sys.exit(1)
    if len(reels) < 2:
        print(f"Not enough filler reels found: {len(reels)}")
        sys.exit(1)
    print(f"Found episodes: {len(episodes)}")
    print(f"Found filler reels: {len(reels)}")
    episode_history = load_episode_history()
    recent_episode_paths = recently_used_episode_paths(episode_history)
    fresh_episodes = [episode for episode in episodes if str(episode) not in recent_episode_paths]
    if len(fresh_episodes) >= 60:
        print(f"Avoiding recently used episodes from the last {EPISODE_HISTORY_BLOCKS} blocks: {len(recent_episode_paths)} tracked")
        episodes = fresh_episodes
    else:
        print(
            f"Warning: only {len(fresh_episodes)} fresh episodes available after history filtering. "
            "Falling back to the full episode pool to complete the block."
        )
    random.shuffle(episodes)
    rows: list[dict] = []
    used_episodes: set[Path] = set()
    used_episode_paths: set[str] = set()
    recent_reels: list[Path] = []
    total = 0.0
    for episode in episodes:
        if total >= MIN_SECONDS:
            break
        if episode in used_episodes:
            continue
        ep_duration = ffprobe_duration(episode)
        if ep_duration <= 60:
            continue
        used_episodes.add(episode)
        used_episode_paths.add(str(episode))
        rows.append(
            {
                "type": "episode",
                "title": label_from_path(episode),
                "path": str(episode),
                "duration": ep_duration,
            }
        )
        total += ep_duration
        selected_reels = choose_reels(reels, recent_reels)
        for reel in selected_reels:
            reel_duration = ffprobe_duration(reel)
            if reel_duration <= 5:
                continue
            rows.append(
                {
                    "type": "filler",
                    "title": "Intermission",
                    "path": str(reel),
                    "duration": reel_duration,
                }
            )
            total += reel_duration
        if total >= TARGET_SECONDS:
            break
    if total < MIN_SECONDS:
        print(f"Could not build enough content. Planned only {total / 3600:.2f} hours.")
        sys.exit(1)
    if total > MAX_SECONDS:
        print(f"Warning: planned duration is {total / 3600:.2f} hours.")
    content_string = "\n".join(row["path"] for row in rows)
    block_hash = hashlib.sha256(content_string.encode("utf-8")).hexdigest()
    block_id = datetime.now(TZ).strftime("%Y%m%d-%H%M%S") + "-" + block_hash[:12]
    block_start = datetime.now(TZ).replace(second=0, microsecond=0)
    print()
    print(f"Planned duration: {total:.2f} seconds = {total / 3600:.2f} hours")
    print(f"Items: {len(rows)}")
    print(f"Episodes used: {len(used_episodes)}")
    print(f"Block ID: {block_id}")
    with PLAN_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["index", "type", "title", "duration", "path"],
        )
        writer.writeheader()
        for i, row in enumerate(rows, start=1):
            writer.writerow(
                {
                    "index": i,
                    "type": row["type"],
                    "title": row["title"],
                    "duration": f"{row['duration']:.3f}",
                    "path": row["path"],
                }
            )
    normalized_paths: list[Path] = []
    for i, row in enumerate(rows, start=1):
        input_path = Path(row["path"])
        output_path = TMP_DIR / f"part_{i:05d}.ts"
        normalize_item(input_path, output_path, f"{i:05d}_{row['type']}")
        normalized_paths.append(output_path)
    with CONCAT_PLAN.open("w", encoding="utf-8") as f:
        for path in normalized_paths:
            f.write(f"file '{path}'\n")
    run(
        [
            "nice", "-n", "19",
            "ionice", "-c2", "-n7",
            "ffmpeg", "-y",
            "-hide_banner",
            "-nostdin",
            "-f", "concat",
            "-safe", "0",
            "-i", str(CONCAT_PLAN),
            "-c", "copy",
            "-hls_time", "4",
            "-hls_list_size", "0",
            "-hls_segment_filename", str(STAGE_DIR / "segment_%05d.ts"),
            "-f", "hls",
            str(STAGE_DIR / "index.m3u8"),
        ]
    )
    write_xmltv(rows, block_start)
    generated_at = datetime.now(TZ).isoformat()
    version_data = {
        "block_id": block_id,
        "generated_at": generated_at,
        "timezone": str(TZ),
        "duration_seconds": round(total, 3),
        "duration_hours": round(total / 3600, 3),
        "item_count": len(rows),
        "episode_count": len(used_episodes),
        "content_hash": block_hash,
        "block_start": block_start.isoformat(),
        "episode_history_blocks": EPISODE_HISTORY_BLOCKS,
    }
    VERSION_OUT.write_text(json.dumps(version_data, indent=2), encoding="utf-8")
    history_path = LOG_DIR / "block_history.csv"
    if publish:
        write_header = not history_path.exists()
        with history_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "block_id",
                    "generated_at",
                    "duration_seconds",
                    "duration_hours",
                    "item_count",
                    "episode_count",
                    "content_hash",
                    "plan_csv",
                    "published_to",
                ],
            )
            if write_header:
                writer.writeheader()
            writer.writerow(
                {
                    "block_id": block_id,
                    "generated_at": version_data["generated_at"],
                    "duration_seconds": version_data["duration_seconds"],
                    "duration_hours": version_data["duration_hours"],
                    "item_count": len(rows),
                    "episode_count": len(used_episodes),
                    "content_hash": block_hash,
                    "plan_csv": str(PLAN_CSV),
                    "published_to": str(LIVE_DIR),
                }
            )
    if publish:
        publish_stage()
        save_episode_history(block_id, generated_at, used_episode_paths)
        print()
        print("Done.")
        print(f"Live HLS: {LIVE_DIR / 'index.m3u8'}")
        print(f"XMLTV: {LIVE_DIR / 'xmltv.xml'}")
        print(f"Version: {LIVE_DIR / 'version.json'}")
        print(f"Plan: {PLAN_CSV}")
        print(f"History: {history_path}")
        print(f"Episode history: {EPISODE_HISTORY_PATH}")
    else:
        print()
        print("Done staging new block.")
        print(f"Staged HLS: {STAGE_DIR / 'index.m3u8'}")
        print(f"Staged XMLTV: {STAGE_DIR / 'xmltv.xml'}")
        print(f"Staged Version: {STAGE_DIR / 'version.json'}")
        print(f"Plan: {PLAN_CSV}")


def parse_live_generated_at() -> datetime | None:
    """Return the generation time of the current live block or None."""
    live_version = LIVE_DIR / "version.json"
    if not live_version.exists():
        return None
    try:
        data = json.loads(live_version.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Warning: could not read live version file {live_version}: {exc}")
        return None
    generated_at = data.get("generated_at") or data.get("block_start")
    if not generated_at:
        return None
    try:
        parsed = datetime.fromisoformat(generated_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def append_publish_history_from_stage() -> None:
    """Append the staged block’s metadata to the history before publish."""
    stage_version = STAGE_DIR / "version.json"
    if not stage_version.exists():
        return
    try:
        version_data = json.loads(stage_version.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Warning: could not read staged version before publish: {exc}")
        return
    history_path = LOG_DIR / "block_history.csv"
    write_header = not history_path.exists()
    with history_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "block_id",
                "generated_at",
                "duration_seconds",
                "duration_hours",
                "item_count",
                "episode_count",
                "content_hash",
                "plan_csv",
                "published_to",
            ],
        )
        if write_header:
            writer.writeheader()
        writer.writerow(
            {
                "block_id": version_data.get("block_id", ""),
                "generated_at": version_data.get("generated_at", ""),
                "duration_seconds": version_data.get("duration_seconds", ""),
                "duration_hours": version_data.get("duration_hours", ""),
                "item_count": version_data.get("item_count", ""),
                "episode_count": version_data.get("episode_count", ""),
                "content_hash": version_data.get("content_hash", ""),
                "plan_csv": str(PLAN_CSV),
                "published_to": str(LIVE_DIR),
            }
        )


def auto_run() -> None:
    """Decide whether to stage or publish based on the age of the live block."""
    last_generated_at = parse_live_generated_at()
    stage_version = STAGE_DIR / "version.json"
    if not last_generated_at:
        print("No valid live version found. Building and publishing immediately.")
        build_block(publish=True)
        return
    now = datetime.now(TZ)
    hours_elapsed = (now - last_generated_at).total_seconds() / 3600.0
    if hours_elapsed >= 24.0:
        if stage_version.exists():
            print(f"Current live block is {hours_elapsed:.2f} hours old. Publishing staged block.")
            append_publish_history_from_stage()
            publish_stage()
            print("Published staged block.")
            return
        print(f"Current live block is {hours_elapsed:.2f} hours old, but no staged block exists. Building and publishing now.")
        build_block(publish=True)
        return
    if hours_elapsed >= 18.0:
        if stage_version.exists():
            print(f"Current live block is {hours_elapsed:.2f} hours old. Staged block already exists. Nothing to do.")
            return
        print(f"Current live block is {hours_elapsed:.2f} hours old. Building next block into staging.")
        build_block(publish=False)
        return
    print(f"Current live block is {hours_elapsed:.2f} hours old. Nothing to do yet.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a 24‑hour HLS channel")
    parser.add_argument("--auto", action="store_true", help="Stage after 18 hours and publish after 24 hours")
    parser.add_argument("--stage-only", action="store_true", help="Build the next block into staging without publishing")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.auto:
        auto_run()
    elif args.stage_only:
        build_block(publish=False)
    else:
        build_block(publish=True)


if __name__ == "__main__":
    main()