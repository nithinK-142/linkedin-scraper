# LinkedIn Scraper

Scrapes and archives LinkedIn saved posts with a real, already-logged-in Chromium profile over CDP.

It saves:
- post text and metadata
- images and other normal media
- videos, including fragmented MP4 streams that LinkedIn serves to the browser

## Requirements

- Python 3.10+
- Brave, Chrome, or Chromium
- LinkedIn already logged in to the selected browser profile
- `ffmpeg` and `ffprobe` for fragmented-video recovery

## Setup

```bash
uv sync
```

Or with standard Python tooling:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

The project does not use Playwright's bundled browser. It attaches to your real Chromium profile over CDP.

## CLI

List browser profiles:

```bash
uv run linkedin-scraper profiles
```

Run the full pipeline. Without `--limit`, it processes all saved posts:

```bash
uv run linkedin-scraper run
uv run linkedin-scraper run --limit 50
```

Stages:

```bash
uv run linkedin-scraper collect
uv run linkedin-scraper archive
uv run linkedin-scraper recover
```

`--limit N` (also `--number N` or `-n N`) limits how many posts each command processes. Without it, all available posts are processed.

Each stage works on its own. `run` passes the same browser/profile through all stages and runs:

```text
collect → archive → recover
```

Browser/profile flags are available on every browser command:

```text
--browser brave|chrome|chromium
--browser-path PATH
--user-data-dir PATH
--profile NAME|NUMBER
--cdp-port PORT
```

Examples:

```bash
uv run linkedin-scraper run --browser brave --profile "Profile 4"
uv run linkedin-scraper recover --url "https://www.linkedin.com/feed/update/urn:li:activity:1234567890"
```

## Recovery

`recover` is for posts that normal archiving cannot fully access, including company/agency posts that hit an authwall or return incomplete data.

It captures normal media from the authenticated browser and keeps the existing fragmented-MP4 logic for videos:

```text
network capture → MP4/fMP4 box detection → fragment reconstruction → ffmpeg → ffprobe
```

Recovery writes media into the post archive:

```text
archive/
└── 0001_<activity_id>/
    ├── metadata.json
    ├── post.md
    ├── media/
    │   ├── ...
    │   └── video.mp4
    └── recovery/
        ├── capture.json
        └── parts/
```

With `--output`, recovery can use a separate output root. The default is the normal `archive/` tree.

## Resume

Progress is stored in `archive/state.sqlite3`. Old `manifest.json` files are imported automatically.

```bash
uv run linkedin-scraper status
```

Re-running skips completed work. Downloads use `.part` files, resume HTTP ranges when supported, retry transient failures, and store SHA-256 hashes.

## Legacy scripts

The old script names still work:

```bash
python scripts/linkedin_saved.py
python scripts/archive_linkedin_posts.py
python scripts/linkedin_video_capture.py
python scripts/run_pipeline.py
python scripts/get_profiles.py
```

They now call the same CLI code.

## Safety

The selected browser profile is never reset, copied, or overwritten. The project does not store LinkedIn credentials, cookies, or session tokens.

LinkedIn's DOM and network behavior can change. The collector and recovery logic may need updates when LinkedIn changes.
