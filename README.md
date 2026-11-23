# Live Stream Auto-Clipper

Find the busiest moments in a YouTube or Twitch VOD's chat replay and save them as MP4 clips **with the same width, height, and aspect ratio as the downloaded source stream**. Nothing is cropped, resized, stretched, or padded: a 1920x1080 source gives 1920x1080 clips.

"Source" means the best video stream yt-dlp can select from YouTube/Twitch (`bv*+ba/b`, sorted by resolution, fps, HDR, codec, bitrate) — not the creator's original upload file.

This project runs **only through GitHub Actions**.

## Run

1. Open **Actions → Live Stream Auto-Clipper → Run workflow**.
2. Enter a recorded `video_url`, `top_clips` (default `5`), and `clip_duration` in seconds (default `45`).
3. When the run finishes, download **shorts-…** from **Artifacts** (kept 7 days). Completed clips are uploaded even if another clip fails.

With the GitHub CLI:

```sh
gh workflow run auto_clipper.yml --ref main -f video_url="https://www.youtube.com/watch?v=VIDEO_ID" -f top_clips=5 -f clip_duration=45
gh run watch RUN_ID
gh run download RUN_ID --dir downloaded_clips
```

## YouTube "Sign in to confirm you're not a bot"

This error is **unrelated to clip dimensions**. YouTube frequently blocks requests from GitHub-hosted runners' datacenter IPs before any video is processed. Twitch VODs are usually unaffected. The reliable workaround is to give yt-dlp signed-in YouTube cookies via an optional repository secret:

1. Preferably use a secondary Google account (YouTube can rate-limit or flag accounts used for automation).
2. Follow yt-dlp's guide to [export YouTube cookies](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies) in Netscape `cookies.txt` format (private window, sign in, export, close the window).
3. In the repository: **Settings → Secrets and variables → Actions → New repository secret**, name `YT_COOKIES`, paste the entire file contents.

The workflow writes the secret to a permission-600 file in the runner's temp directory, passes it to yt-dlp and chat-downloader, and deletes it after the run. GitHub masks secret values in logs; the script logs only whether cookies are in use. Never commit cookies to the repository. Cookies expire; if the error returns, re-export and update the secret. Even with cookies, YouTube may still block some runner IPs — a self-hosted runner on a residential connection is the alternative.

## How it works

- Stream the full chat replay into 30-second buckets and rank by comment count (ties → earlier).
- Greedily pick the highest buckets, skipping any whose clip would overlap an already selected clip.
- Each clip starts 10 seconds before its spike bucket (clamped to 0) and lasts the requested duration (or until the VOD ends).
- Only that time range is downloaded via yt-dlp `--download-sections` with `--force-keyframes-at-cuts`; there is no whole-VOD fallback.
- FFmpeg re-encodes to an exact-length H.264/AAC MP4 (CRF 18, fast start) **without any scale/crop/pad filter**; the source frame rate and sample aspect ratio are kept. Even dimensions use yuv420p; odd dimensions use yuv444p so the size still matches. Silent sources get a silent AAC track.
- ffprobe checks every clip: width, height and SAR must equal the source segment's, codecs must be H.264/AAC, and duration within 0.5 s. Logs show `Source stream: WxH` and `Output clip: WxH` per clip.
- Raw and partial files are removed automatically; output goes to `output_shorts/` and is uploaded with `actions/upload-artifact@v4`.

Large sources (e.g. 4K) produce large files and take longer to encode; the job limit is 180 minutes.

## Tests

The workflow runs `python -m unittest discover -s tests -v` before clipping. Media tests generate synthetic footage (1920x1080, 640x360, portrait 180x400, odd 641x361, anamorphic 720x480 with SAR 32:27) and verify via ffprobe that output dimensions and SAR equal the input. Provider access (YouTube/Twitch) needs a real accessible VOD and is not covered by tests.

References: [yt-dlp](https://github.com/yt-dlp/yt-dlp#usage-and-options), [chat-downloader](https://chat-downloader.readthedocs.io/en/latest/), [workflow dispatch](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onworkflow_dispatch).
