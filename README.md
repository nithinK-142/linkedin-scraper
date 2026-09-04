# LinkedIn Saved Posts Archiver

Archives your LinkedIn saved posts — text, author, media, and (for the
posts that misbehave) video — by driving a real, already-logged-in
Chromium-family browser profile over the Chrome DevTools Protocol (CDP).

## 1. What this project does

Three stages, each independently runnable:

1. **Collect** — visits `my-items/saved-posts/`, captures network
   responses, and extracts every saved post's URL, in the order LinkedIn
   exposes them.
2. **Archive** — opens each URL, locates the actual post by its activity
   ID (not just "the first article on the page" — that can grab a
   comment instead), and saves author, text, timestamp, and media.
3. **Video capture** — a fallback for posts (typically from company/org
   pages) that authwall or return empty data through the normal archiver
   even though they load fine in a real logged-in profile. It captures
   the real fragmented-MP4 network traffic while the video plays and
   reconstructs it with ffmpeg.

## 2. Important browser constraint

This is **not** a generic "works with any browser" tool. It is
specifically built on Chromium/CDP, because the whole point is to reuse
an existing, already-authenticated browser session rather than log in
again. Supported browsers: **Brave, Google Chrome, Chromium** (any other
Chromium-based browser will likely work if you point `--browser-path`
and `--user-data-dir` at it, but only these three are auto-detected).
Firefox is not, and will not be, supported by this architecture.

## 3. Requirements

- Python 3.10+
- A supported Chromium-family browser, already logged into LinkedIn in
  some profile
- `ffmpeg` and `ffprobe` (system packages, not Python packages — used
  only by the video-capture stage)

## 4. Setup

```bash
git clone <this-repo>
cd linkedin-archiver

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
```

**Playwright's own bundled Chromium is not needed and `playwright install
chromium` is not required.** This project only ever attaches to your real,
already-installed browser over CDP — it never launches Playwright's
bundled browser as a replacement.

### ffmpeg

Only needed for the video-capture stage (`scripts/linkedin_video_capture.py`).

```bash
# Fedora
sudo dnf install ffmpeg

# Debian/Ubuntu
sudo apt install ffmpeg
```

The script checks for `ffmpeg`/`ffprobe` at startup and fails with a clear
message if they're missing. Nothing is installed automatically.

## 5. Browser and profile discovery

You never type a hardcoded profile name. List what's available:

```bash
python scripts/get_profiles.py
```

```
Brave — /home/you/.config/BraveSoftware/Brave-Browser

  1. Personal  (you@example.com)  [dir: Default]
  2. LinkedIn  [dir: Profile 4]
```

Every script accepts the same selection flags:

```
--browser        brave | chrome | chromium | 1 | 2 | ...
--browser-path   explicit path to the executable
--user-data-dir  explicit path to the user-data directory
--profile        profile directory name, display name, or number
--cdp-port       explicit CDP port (default: 9222, or the next free port)
```

Omit `--browser`/`--profile` and you'll be prompted interactively instead.

## 6. How LinkedIn login works here

There's no login step in this project. You log into LinkedIn normally, in
your browser, in whichever profile you'll select. The scripts attach to
that already-authenticated session over CDP — they never see, store, or
touch your credentials, cookies, or session tokens.

**Session safety**, enforced everywhere: the selected profile is never
overwritten, deleted, or reset; cookies are never copied to another
profile; a throwaway profile is never created. If the browser is already
running on the target profile *without* a debugging port open, the
scripts refuse to relaunch it (Chromium only honors
`--remote-debugging-port` on the first launch of a given user-data-dir) —
you'll be told to quit it fully and re-run. If something is already
listening on the CDP port, you're asked to confirm it's actually showing
the profile you selected, since that can't be verified programmatically.

## 7. First-time setup

1. Open your browser, log into LinkedIn in the profile you want to use.
2. `python scripts/get_profiles.py` to confirm the profile directory name.
3. Run stage 1 (below). It will offer to launch the browser with
   debugging enabled if it isn't already running that way.

## 8. Collecting saved-post URLs (Stage 1)

```bash
python scripts/linkedin_saved.py --browser brave --profile "Profile 4"
```

Scrolls the saved-posts page, capturing activity URNs from network
responses (DOM scraping is only a fallback). Order is preserved — URLs
are **never** sorted. Output is saved continuously, so Ctrl+C or a
browser hiccup never loses progress.

Default output: `data/linkedin_saved_posts.json`. Override with `--output`.

## 9. Archiving posts (Stage 2)

```bash
python scripts/archive_linkedin_posts.py \
    --input data/linkedin_saved_posts.json \
    --output archive \
    --resume
```

For each URL: locates the real post via its activity ID, expands "See
more", and saves author/username/profile URL/timestamp/text/media.

Output layout:

```
archive/
    manifest.json
    0001_7500775585367867392/
        metadata.json
        post.md
        media/
    0002_7499728147685339136/
        ...
```

`manifest.json` tracks status per post (`completed`, `failed`,
`login_required`, `challenge`, `unavailable`, `extraction_failed`,
`media_failed`) plus author, media count, and error details. Re-running
skips anything already `completed` — nothing is reprocessed from zero.

## 10. Capturing problem videos (Stage 3)

```bash
python scripts/linkedin_video_capture.py \
    --url https://www.linkedin.com/feed/update/urn:li:activity:1234567890/ \
    --profile linkedin
```

or process everything from a file:

```bash
python scripts/linkedin_video_capture.py --input data/linkedin_saved_posts.json
python scripts/linkedin_video_capture.py --input data/failed_posts.json
```

Nothing is hardcoded — no test URLs, no fixed post count. Output:

```
archive/videos/
    manifest.json
    <activity_id>/
        video.mp4              # only written once ffprobe confirms it's valid
        capture.json           # every response captured, for debugging
        parts/                 # raw captured fragments
```

If reconstruction can't be verified, you'll get `video.mp4.UNVERIFIED` or
`video.mp4.FAILED` instead of a silently "successful" broken file — the
script never treats "a file exists" as proof it worked.

## 11. Optional: run everything in one go

```bash
python scripts/run_pipeline.py --browser brave --profile "Profile 4"
```

Runs Stage 1, then Stage 2 with `--resume`, then automatically feeds
whatever's left unresolved into Stage 3. Each script remains fully usable
on its own; this is just a convenience wrapper.

## 12. Resume behavior

Every stage is resumable via its own `manifest.json`. Interrupting with
Ctrl+C is safe — already-completed items stay completed, and the next run
picks up where it left off.

## 13. Logs

Every script writes both to the console and to its own log file:

```
logs/linkedin_saved.log
logs/archive_linkedin_posts.log
logs/linkedin_video_capture.log
logs/get_profiles.log
logs/run_pipeline.log
```

Logs never contain cookies, auth headers, session tokens, or credentials.

## 14. Configuration

Stable, non-secret settings (CDP port, timeouts) can go in an optional
`config.json` at the project root — see `config.example.json`. CLI flags
always override it. There is no `.env` file and no environment-variable
configuration; use a config file or CLI flags. No credentials or session
data belong in `config.json` — there's nothing to put there, since this
project never handles credentials at all.

## 15. Troubleshooting

- **"LinkedIn is not logged in on this profile"** — log in, in that
  browser profile, then re-run.
- **"already running... without a remote-debugging port"** — fully quit
  the browser (all windows) and re-run; Chromium only opens a debug port
  on the first launch of a user-data-dir.
- **`video.mp4.FAILED` / no init segment captured** — the page likely
  didn't actually start playing the video (autoplay blocked and no
  manual click happened in time), or LinkedIn changed how it delivers
  the stream. Check `capture.json` for what was actually captured.
- **ffmpeg/ffprobe not found** — install the system package (see above);
  these are not pip-installable.

## 16. Known limitations

- Chromium-family only; no Firefox/WebKit support.
- Video capture requires the video to actually play in-browser during the
  run (autoplay, or a manual click if autoplay is blocked) — it cannot
  fetch a video LinkedIn hasn't streamed yet.
- LinkedIn's DOM and network shapes change over time; the CSS
  selectors/box-detection heuristics here reflect what was observed
  working at the time of writing and may need updates.
- Profile-matching for an already-running browser on the CDP port relies
  on human confirmation, not a programmatic check — the CDP protocol
  doesn't expose which on-disk profile a running instance loaded.

## 17. What changed from the original scripts

- Browser hardcoded to Brave everywhere → generic Chromium-family
  detection/selection (`browser_discovery.py`, `profiles.py`), with CLI
  overrides for anyone on a browser we don't auto-detect.
- `get_brave_profiles.py`'s Brave-only `Local State` parsing → reusable
  `profiles.discover_profiles()` used by every script.
- Hardcoded CDP port 9222 → dynamic selection that reuses a live CDP
  endpoint on the preferred port, or finds a free one nearby.
- Duplicated URL normalization / activity-ID regex across all three
  scripts → single `linkedin_urls.py`.
- Duplicated Brave-launch/attach logic (each script had its own
  `ensure_brave` variant) → single `browser.py`, generalized to any
  supported browser, using the most defensive version of the safety
  checks (from the video-capture script) as the baseline everywhere.
- `linkedin_video_capture.py`'s two hardcoded test URLs → `--url`
  (repeatable) and `--input` (any JSON list of URLs).
- No manifest/resume in the original archiver → per-post status tracking
  with explicit failure states, safe to interrupt and rerun.
- Config constants scattered through each script → `config.py` +
  optional `config.json`.
- No structured logging in the originals (console `print()` only) →
  per-script log files plus console output.

## 18. What was preserved because it already worked

- Network-response-based saved-post collection, with DOM scanning as a
  fallback only (not the primary method).
- Preserving saved-post order — never sorted.
- Locating the main post by activity ID rather than `article.first`.
- The video-capture approach in full: network interception (never the
  `blob:` URL), MP4/fMP4 box-signature detection rather than
  content-type/URL guessing, explicit initialization-segment
  verification before claiming success, and ffprobe validation of the
  final file.

## 19. Unavoidable limitations

- There is no fully generic way to verify a running browser process is
  showing the exact profile you asked for — the CDP `/json/version`
  endpoint doesn't expose that, so this project asks for human
  confirmation in that one case rather than guessing.
- LinkedIn is not a public API; both extraction and video capture depend
  on the current DOM/network shape of linkedin.com and may need
  maintenance as LinkedIn changes it.
