# LinkedIn Scraper

Scrapes and archives LinkedIn saved posts with a real, already-logged-in Chromium profile over CDP.

It saves:
- post text and metadata
- attached media only
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

`--sleep N` waits N seconds between LinkedIn page/post operations. Default: disabled.
`--verbose` (or `-v`) shows debug logs and stores the same debug output in the command log.

Each stage works on its own. `run` passes the same browser/profile through all stages and runs:

```text
collect → archive → recover
```

For `run`, the full pipeline is written to `logs/run.log`. The file mirrors console output unless `--verbose` is enabled.

## Config

Edit `config.toml` to keep defaults in one place. Uncomment only the settings you need. CLI flags override config values.

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

`recover` retries posts that normal archiving could not fully access, including company/agency posts that hit an authwall or return incomplete data. It first locates the target post, saves its content and metadata, then downloads only media belonging to that post.

Fragmented video still uses the existing network capture path:

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
`recover` without `--url` or `--input` reads unresolved posts directly from SQLite. It does not create a separate `failed_posts.json`.

## Safety

The selected browser profile is never reset, copied, or overwritten. The project does not store LinkedIn credentials, cookies, or session tokens.

LinkedIn warns that systematic automated page retrieval can trigger restrictions, including limits after unusually large page-view volume. The scraper supports pacing, stops page processing on HTTP 429 or restriction/security signals, and waits longer before retrying a media 429. This reduces burst traffic; it does not bypass LinkedIn restrictions.

LinkedIn's DOM and network behavior can change. The collector and recovery logic may need updates when LinkedIn changes.
