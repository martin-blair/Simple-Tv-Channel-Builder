# Simple TV Channel Builder

This repository demonstrates a **minimal** approach to running your own
linear TV channel using nothing more than a Python script, ffmpeg and a
simple web page.  It was inspired by the workflow described in our
project: instead of relying on heavy IPTV frameworks or media servers
like Tunarr or ErsatzTV, this stack builds a 24‑hour block of content
from a local media library, stitches on short bumper/filler clips,
generates an [HLS](https://en.wikipedia.org/wiki/HTTP_Live_Streaming)
playlist on disk, and exposes a programme guide in XMLTV format.  A
small JSON and CSV history tracks which episodes were recently aired so
the scheduler avoids repeating them for a configurable number of
days.  Once generated, the files can be served by any static web
server (e.g. nginx or Cloudflare Pages) and viewed in a browser with
no special plugins.

## Repository layout

- `builder/` – the Python script that assembles, normalizes and
  concatenates media into 24‑hour blocks.  It writes out the HLS
  playlist (`index.m3u8` plus `.ts` segments), an XMLTV guide
  (`xmltv.xml`) and a `version.json` manifest containing metadata
  (block id, duration, timestamp).  The script supports three modes
  controlled by command‑line flags:

  * **Immediate publish** (default) – build a fresh block and publish
    it directly to the live directory.
  * **Stage only** (`--stage-only`) – build a block into a staging
    directory without replacing the live stream.  This is useful for
    preparing tomorrow’s broadcast.
  * **Auto** (`--auto`) – examine the timestamp of the current live
    block and decide whether to stage or publish.  If 18 hours have
    elapsed since the last publish, it builds into staging.  If 24
    hours have passed, it publishes the staged block and starts the
    next cycle.

  See [`builder/build_block.py`](builder/build_block.py) for details.

- `web/` – a minimal HTML front end that plays the HLS stream in a
  `<video>` element via [hls.js](https://github.com/video-dev/hls.js)
  and monitors `version.json` every minute.  When it detects a new
  `block_id` it automatically reloads the page so viewers are
  seamlessly switched to the next day’s content.  It also fetches the
  programme guide from `xmltv.xml` (not displayed by default but
  available for clients that need it).


## How it works

1. **Collect media** – the script walks a list of `SOURCE_DIRS` for
   episodes (e.g. seasons of shows) and `REEL_DIRS` for short
   bumpers/promos.  All files with recognised video extensions are
   considered.  If fewer than a configurable number of episodes or
   fillers are found the build aborts.

2. **Avoid repeats** – a JSON file under `logs/` tracks the file
   paths used in the last *N* blocks (defaults to 7).  When building
   a new block the script filters out any episodes seen in the recent
   history if enough fresh episodes remain.  This simple rule helps
   avoid back‑to‑back reruns even with a modest library.

3. **Create a plan** – episodes and randomly selected filler reels
   are shuffled and appended until at least 23.75 hours of content
   exist.  The plan is saved to `test_24h_plan.csv` for debugging.

4. **Normalize** – each chosen file is transcoded via ffmpeg
   (resolution, framerate, audio bitrate) into MPEG‑TS format.  A
   second ffmpeg run concatenates the pieces into an HLS master
   playlist with 4‑second segments.

5. **Generate metadata** – an XMLTV guide is written with start and
   stop times for each programme.  A `version.json` file contains the
   block id (based on timestamp and a hash of the content list), the
   ISO timestamp of generation, duration, item counts and other
   metadata.  This file is used both by the webpage for reload
   detection and by cron jobs to know when to stage/publish.

6. **Staging and publishing** – live files are served from a folder
   such as `www/channel-live`.  When a new block is ready it is
   generated in `www/channel-stage`.  Publishing atomically renames
   the staging folder over the live folder, preserving a single
   previously live block as a backup (which is then removed).  This
   avoids partial updates or segments being swapped midstream.

7. **Automation** – run the script every 15 minutes via cron with
   `--auto`.  It will stage a new block after 18 hours and publish it
   after 24 hours without overlapping builds.  On Linux you can add
   this line to your crontab:

   ```cron
   */15 * * * * /usr/bin/flock -n /tmp/channel_builder.lock /usr/bin/python3 /path/to/repo/builder/build_block.py --auto >> /path/to/repo/builder/logs/auto.log 2>&1
   ```

## Deployment notes

- **Paths** – adjust the `SOURCE_DIRS`, `REEL_DIRS`, `WORK_DIR`,
  `LIVE_DIR` and `STAGE_DIR` constants in `builder/build_block.py` to
  match your environment.  They point at your media library, the
  folders where ffmpeg writes temporary files and where your web
  server will host the stream.  The repository intentionally avoids
  embedding personal server hostnames.
- **Transcoding** – ffmpeg must be installed on the system and
  accessible on the `PATH`.  The current settings produce 720p HLS at
  around 2.5 Mbps with audio at 160 kbps.  You can tune the
  `-crf`, `-maxrate`, `-bufsize` and other options in the
  `normalize_item` function.
- **Web server** – serve the contents of `web/` and the output of
  your build directories (`LIVE_DIR`, `STAGE_DIR`) with a static
  server.  The provided `index.html` expects to find `index.m3u8`,
  `xmltv.xml` and `version.json` in the same directory.  You can
  deploy this on any platform that can serve static files over
  HTTP/HTTPS.
- **Security** – this code is provided for educational purposes.  It
  does not include authentication, DRM or encryption.  Use it only for
  legally acquired media in accordance with copyright law.

## License

This project is released under the MIT license.  See [LICENSE](LICENSE)
for details.
