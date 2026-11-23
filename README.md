# Live Stream Auto-Clipper

Find the busiest moments in a YouTube or Twitch VOD's chat replay and turn them into 1080x1920 vertical MP4s for Shorts, Reels, and TikTok.

## Run on GitHub (no local installation)

1. Open **Actions → Live Stream Auto-Clipper → Run workflow** in this repository.
2. Enter a recorded `video_url`, `top_clips` (default `5`), and `clip_duration` in seconds (default `45`). Select **Run workflow**.
3. Open the completed run and download **shorts-…** from **Artifacts**. Extract the ZIP to get your MP4s. Artifacts are kept for 7 days.

The workflow must exist on the repository's default branch, and Actions must be enabled. Runs use GitHub-hosted compute and count toward your account's Actions allowance. A job has a 180-minute limit. Successfully completed clips are uploaded even if another clip fails.

With an authenticated GitHub CLI, run from the repository directory:

```sh
gh workflow run auto_clipper.yml --ref main -f video_url="https://www.youtube.com/watch?v=VIDEO_ID" -f top_clips=5 -f clip_duration=45
gh run list --workflow auto_clipper.yml
gh run watch RUN_ID
gh run download RUN_ID --dir downloaded_shorts
```

Use a real video ID. If necessary, authenticate first with `gh auth login --hostname github.com`.

## Run locally

Install **Python 3.11+**, **FFmpeg including ffprobe**, and **Node.js 22+ or Deno 2+** on PATH. On Ubuntu, FFmpeg is available through `sudo apt-get install ffmpeg`.

```sh
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell instead:
# .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python auto_clipper.py --url "https://www.twitch.tv/videos/123456789" --clips 5 --duration 45
```

Results appear in `output_shorts/` relative to your working directory. Each run uses unique filenames and preserves earlier clips. `--clips` and `--duration` must be positive integers. Exit codes: `0` success, `1` processing failure (possibly with usable partial results), `2` invalid arguments, `130` interruption.

## How selection and downloads work

- Stream the full chat replay into fixed 30-second buckets. Rank by comment count, breaking ties chronologically. No comment text is saved.
- Greedily select the highest-count bucket, then skip any bucket whose clip would overlap an already selected clip. This prioritizes individual spikes, not maximum total comments across a combination of clips. Fewer clips may be available than requested.
- Start each clip 10 seconds before the **start of its spike bucket**, clamped to zero. End after the requested duration or at the VOD's end, whichever comes first. Very short durations may finish before the spike; 45 seconds covers the full bucket and lead-in.
- Request each time range through yt-dlp's `--download-sections`, with FFmpeg and keyframe correction. There is no whole-VOD fallback. Providers may transfer neighboring keyframes/fragments; exact network byte savings depend on the source's seeking support.
- Center-crop, encode at 1080x1920/30 fps (H.264, yuv420p, AAC stereo), and enable MP4 fast start. Silent sources receive a silent AAC track. Probe each output before publishing it, and automatically remove raw/partial files on normal completion, failure, or Ctrl+C.

## Requirements and troubleshooting

Use completed, accessible VODs with chat replay. Currently broadcasting streams, channel URLs, deleted VODs, unavailable chat, and login/region-restricted recordings are not supported. YouTube can reject GitHub runner IPs or require sign-in; that appears as an extraction error rather than an empty successful run. This project does not bypass provider access controls. Center-cropping can remove off-center subjects, overlays, or gameplay UI.

Provider APIs change independently of this project. Direct dependency versions are pinned in `requirements.txt`; update and retest them if extraction breaks. A forced process termination or machine shutdown can leave a hidden `.auto_clipper_*` temporary directory under `output_shorts/`; remove it only after confirming no clipper run is active.

## Tests

```sh
python -m unittest discover -s tests -v
```

Unit tests run offline. Media integration tests use generated footage and run when FFmpeg and ffprobe are available; the Actions workflow installs both. Provider availability requires a real accessible VOD and is separate from these tests.

Implementation references: [yt-dlp](https://github.com/yt-dlp/yt-dlp#usage-and-options), [chat-downloader](https://chat-downloader.readthedocs.io/en/latest/), [workflow dispatch](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onworkflow_dispatch).
