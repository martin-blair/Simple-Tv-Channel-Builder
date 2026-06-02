#!/usr/bin/env python3
import csv
import hashlib
import json
import random
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape

TZ = ZoneInfo("America/Chicago")

TARGET_SECONDS = 24 * 60 * 60
MIN_SECONDS = int(23.75 * 60 * 60)
MAX_SECONDS = int(24.25 * 60 * 60)

WORK_DIR = Path("/path/to/channel_builder")
TMP_DIR = WORK_DIR / "work_24h_test"
LOG_DIR = WORK_DIR / "logs"

LIVE_DIR = Path("/path/to/public/live")
STAGE_DIR = Path("/path/to/public/stage")

REEL_DIRS = [
    Path("/path/to/filler/reels/category_1"),
    Path("/path/to/filler/reels/category_2"),
]

SOURCE_DIRS = [
    Path("/path/to/media/source_1"),
    Path("/path/to/media/source_2"),
    Path("/path/to/media/source_3"),
    Path("/path/to/media/source_4"),
    Path("/path/to/media/source_5"),
    Path("/path/to/media/source_6"),
    Path("/path/to/media/source_7"),
    Path("/path/to/media/source_8"),
]

VIDEO_EXTS = {".mp4", ".mkv", ".m4v", ".avi", ".mov", ".webm"}

PLAN_CSV = WORK_DIR / "test_24h_plan.csv"
CONCAT_PLAN = WORK_DIR / "test_24h_concat.txt"
XMLTV_OUT = STAGE_DIR / "xmltv.xml"
VERSION_OUT = STAGE_DIR / "version.json"

EPISODE_HISTORY_PATH = LOG_DIR / "used_episodes_history.json"
EPISODE_HISTORY_BLOCKS = 7


def run(cmd):
    print()
    print(" ".join(str(x) for x in cmd))
    subprocess.run(cmd, check=True)


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
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


def collect_videos(paths):
    files = []

    for root in paths:
        if not root.exists():
            print(f"Skipping missing source: {root}")
            continue

        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in VIDEO_EXTS:
                files.append(path)

    return sorted(set(files))


def label_from_path(path: Path) -> str:
    text = path.name
    text = text.rsplit(".", 1)[0]
    text = text.replace(".", " ")
    text = text.replace("_", " ")
    text = " ".join(text.split())
    return text[:120]


def xmltv_time(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S %z")


def clean_dir(path: Path):
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def load_episode_history():
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

    cleaned_blocks = []
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

    return {"blocks": cleaned_blocks[-EPISODE_HISTORY_BLOCKS:]}


def recently_used_episode_paths(history):
    recent = set()

    for block in history.get("blocks", []):
        for path in block.get("episodes", []):
            recent.add(path)

    return recent


def save_episode_history(block_id, generated_at, used_episode_paths):
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

    tmp_path = EPISODE_HISTORY_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
    tmp_path.replace(EPISODE_HISTORY_PATH)


def remember_selected_reels(selected, recent_reels):
    for reel, _duration in selected:
        recent_reels.append(reel)

    while len(recent_reels) > 25:
        recent_reels.pop(0)


def choose_reels(reels, recent_reels, allow_long_break=False):
    """
    Normal break:
      - Pick 1 reel, max 5 minutes.

    Occasional long break:
      - Pick 2 reels only if their combined runtime is over 5 minutes
        but no more than 8 minutes.
      - The caller controls how often long breaks are allowed.
    """
    MAX_SINGLE_REEL_SECONDS = 5 * 60
    MAX_TWO_REEL_SECONDS = 8 * 60

    available = [r for r in reels if r not in recent_reels]
    if not available:
        available = reels[:]

    random.shuffle(available)

    duration_cache = {}

    def duration_for(path):
        if path not in duration_cache:
            duration_cache[path] = ffprobe_duration(path)
        return duration_cache[path]

    single_reel_options = [
        (reel, duration_for(reel))
        for reel in available
        if 5 < duration_for(reel) <= MAX_SINGLE_REEL_SECONDS
    ]

    two_reel_options = []

    if allow_long_break:
        candidates = [
            (reel, duration_for(reel))
            for reel in available
            if 5 < duration_for(reel) <= MAX_SINGLE_REEL_SECONDS
        ]

        for i, first in enumerate(candidates):
            for second in candidates[i + 1:]:
                total_duration = first[1] + second[1]
                if MAX_SINGLE_REEL_SECONDS < total_duration <= MAX_TWO_REEL_SECONDS:
                    two_reel_options.append([first, second])

    if two_reel_options and random.choice([True, False]):
        selected = random.choice(two_reel_options)
    elif single_reel_options:
        selected = [random.choice(single_reel_options)]
    elif two_reel_options:
        selected = random.choice(two_reel_options)
    else:
        selected = []

    remember_selected_reels(selected, recent_reels)
    return selected


def normalize_item(input_path: Path, output_path: Path, label: str):
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


def write_xmltv(rows, block_start):
    programmes = []
    current = block_start

    for row in rows:
        start = current
        stop = current + timedelta(seconds=row["duration"])
        current = stop

        title = row["title"]
        desc = "Toonami Intermission" if row["type"] == "filler" else ""

        programmes.append(
            f'''  <programme start="{xmltv_time(start)}" stop="{xmltv_time(stop)}" channel="toonami-after-hours">
    <title lang="en">{escape(title)}</title>
    <desc lang="en">{escape(desc)}</desc>
  </programme>'''
        )

    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<tv generator-info-name="Toonami Builder">
  <channel id="toonami-after-hours">
    <display-name>Toonami After Hours</display-name>
  </channel>
{chr(10).join(programmes)}
</tv>
'''

    XMLTV_OUT.write_text(xml, encoding="utf-8")


def publish_stage():
    old_dir = LIVE_DIR.with_name("toonami-live-old")

    if old_dir.exists():
        shutil.rmtree(old_dir)

    if LIVE_DIR.exists():
        LIVE_DIR.rename(old_dir)

    STAGE_DIR.rename(LIVE_DIR)

    print()
    print(f"Published new block to {LIVE_DIR}")

    if old_dir.exists():
        shutil.rmtree(old_dir)


def write_publish_history(version_data):
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

    return history_path


def build_block(publish=True):
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

    rows = []
    used_episodes = set()
    used_episode_paths = set()
    recent_reels = []
    episodes_since_long_break = 4
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

        selected_reels = choose_reels(
            reels,
            recent_reels,
            allow_long_break=episodes_since_long_break >= 4,
        )

        break_duration = sum(reel_duration for _reel, reel_duration in selected_reels)

        for reel, reel_duration in selected_reels:
            rows.append(
                {
                    "type": "filler",
                    "title": "Toonami Intermission",
                    "path": str(reel),
                    "duration": reel_duration,
                }
            )
            total += reel_duration

        if break_duration > 5 * 60:
            episodes_since_long_break = 0
        else:
            episodes_since_long_break += 1

        if total >= TARGET_SECONDS:
            break

    if total < MIN_SECONDS:
        print(f"Could not build enough content. Planned only {total / 3600:.2f} hours.")
        sys.exit(1)

    if total > MAX_SECONDS:
        print(f"Warning: planned duration is {total / 3600:.2f} hours.")

    content_string = "\n".join(row["path"] for row in rows)
    block_hash = hashlib.sha256(content_string.encode("utf-8")).hexdigest()
    block_start = datetime.now(TZ).replace(second=0, microsecond=0)
    block_id = block_start.strftime("%Y%m%d-%H%M%S") + "-" + block_hash[:12]

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

    normalized_paths = []

    for i, row in enumerate(rows, start=1):
        input_path = Path(row["path"])
        output_path = TMP_DIR / f"part_{i:05d}.ts"
        normalize_item(input_path, output_path, f"{i:05d}_{row['type']}")
        normalized_paths.append(output_path)

    with CONCAT_PLAN.open("w", encoding="utf-8") as f:
        for path in normalized_paths:
            f.write(f"file '{path}'\n")

    segment_pattern = STAGE_DIR / f"segment_{block_id}_%05d.ts"

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
            "-hls_segment_filename", str(segment_pattern),
            "-f", "hls",
            str(STAGE_DIR / "index.m3u8"),
        ]
    )

    write_xmltv(rows, block_start)

    generated_at = datetime.now(TZ).isoformat()

    version_data = {
        "block_id": block_id,
        "generated_at": generated_at,
        "timezone": "America/Chicago",
        "duration_seconds": round(total, 3),
        "duration_hours": round(total / 3600, 3),
        "item_count": len(rows),
        "episode_count": len(used_episodes),
        "content_hash": block_hash,
        "block_start": block_start.isoformat(),
        "episode_history_blocks": EPISODE_HISTORY_BLOCKS,
        "segment_prefix": f"segment_{block_id}_",
    }

    VERSION_OUT.write_text(json.dumps(version_data, indent=2), encoding="utf-8")

    if publish:
        history_path = write_publish_history(version_data)
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


def parse_live_generated_at():
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


def append_publish_history_from_stage():
    stage_version = STAGE_DIR / "version.json"

    if not stage_version.exists():
        return

    try:
        version_data = json.loads(stage_version.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Warning: could not read staged version before publish: {exc}")
        return

    write_publish_history(version_data)


def save_episode_history_from_stage():
    stage_version = STAGE_DIR / "version.json"

    if not stage_version.exists():
        return

    try:
        version_data = json.loads(stage_version.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Warning: could not read staged version before episode history update: {exc}")
        return

    block_id = version_data.get("block_id", "")
    generated_at = version_data.get("generated_at", datetime.now(TZ).isoformat())

    if not PLAN_CSV.exists():
        print(f"Warning: plan CSV not found, episode history not updated: {PLAN_CSV}")
        return

    used_episode_paths = set()

    try:
        with PLAN_CSV.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("type") == "episode" and row.get("path"):
                    used_episode_paths.add(row["path"])
    except Exception as exc:
        print(f"Warning: could not read plan CSV for episode history: {exc}")
        return

    if used_episode_paths:
        save_episode_history(block_id, generated_at, used_episode_paths)


def auto_run():
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
            save_episode_history_from_stage()
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


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(description="Build Toonami After Hours 24-hour HLS blocks")
    parser.add_argument("--auto", action="store_true", help="Stage after 18 hours and publish after 24 hours")
    parser.add_argument("--stage-only", action="store_true", help="Build the next block into staging without publishing")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.auto:
        auto_run()
    elif args.stage_only:
        build_block(publish=False)
    else:
        build_block(publish=True)


if __name__ == "__main__":
    main()
