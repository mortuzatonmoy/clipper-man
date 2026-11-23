#!/usr/bin/env python3
"""Turn chat replay spikes from a recorded YouTube/Twitch VOD into vertical clips."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Iterable
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

LOG = logging.getLogger("auto_clipper")
BUCKET_SECONDS = 30
PRE_ROLL_SECONDS = 10
OUTPUT_DIR = Path("output_shorts")


class ClipperError(RuntimeError):
    """An actionable pipeline failure."""


@dataclass(frozen=True)
class Moment:
    bucket: int
    comments: int
    start: float
    end: float


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def vod_url(value: str) -> str:
    """Accept video URLs only; remove playlists, tracking, and start-time hints."""
    try:
        parts = urlsplit(value.strip())
        if (parts.scheme not in {"http", "https"} or parts.username
                or parts.password or parts.port):
            raise ValueError
        host = (parts.hostname or "").lower()
        path = parts.path.rstrip("/")
        video_id = None
        if host == "youtu.be":
            video_id = path.lstrip("/")
        elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            if path == "/watch":
                video_id = parse_qs(parts.query).get("v", [""])[0]
            elif re.fullmatch(r"/(live|embed|shorts)/[A-Za-z0-9_-]{11}", path):
                video_id = path.rsplit("/", 1)[1]
        elif host in {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}:
            if re.fullmatch(r"/videos/[0-9]+", path):
                return "https://www.twitch.tv" + path
        if video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return "https://www.youtube.com/watch?v=" + video_id
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(
        "use a YouTube video URL or https://www.twitch.tv/videos/123456789; "
        "channel URLs and streams still broadcasting are not supported"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, type=vod_url, help="Recorded VOD URL")
    parser.add_argument("--clips", type=positive_int, default=5, help="Maximum clips (default: 5)")
    parser.add_argument("--duration", type=positive_int, default=45,
                        help="Clip length in seconds (default: 45)")
    return parser.parse_args(argv)


def check_dependencies(url: str) -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise ClipperError("Install FFmpeg and put ffmpeg/ffprobe on PATH. Missing: " + ", ".join(missing))
    for module in ("yt_dlp", "chat_downloader"):
        if importlib.util.find_spec(module) is None:
            raise ClipperError("Install dependencies: python -m pip install -r requirements.txt")
    if "youtube.com" in url and not any(shutil.which(name) for name in ("node", "deno")):
        raise ClipperError("YouTube extraction needs Node.js 22+ or Deno 2+ on PATH.")


def run_command(command: list[str], label: str, timeout: int,
                capture: bool = False) -> str:
    """Run without a shell; subprocess logs remain visible in Actions."""
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else None,
            encoding="utf-8", errors="replace",
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        raise ClipperError(f"Cannot run {label}: {exc}") from exc
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        # yt-dlp spawns FFmpeg: stopping only its parent leaves downloads running.
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=15, check=False)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise ClipperError(f"{label} timed out after {timeout}s") from exc
    if process.returncode:
        raise ClipperError(f"{label} failed (exit {process.returncode}); see tool logs above")
    return stdout or ""


def ytdlp_command() -> list[str]:
    return [
        sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-plugin-dirs",
        "--no-playlist", "--no-cache-dir", "--js-runtimes", "node",
        "--socket-timeout", "30", "--retries", "3", "--fragment-retries", "3",
        "--extractor-retries", "3",
    ]


def get_vod_duration(url: str) -> float:
    raw = run_command(ytdlp_command() + ["--skip-download", "--dump-single-json", "--", url],
                      "VOD metadata lookup", 180, capture=True)
    try:
        info = json.loads(raw)
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
            raise ClipperError("Wait until the broadcast has ended and the VOD and chat replay are processed.")
        duration = float(info.get("duration") or 0)
        if not math.isfinite(duration) or duration <= 0:
            raise ClipperError("The VOD has no usable duration; it may still be processing or unavailable.")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ClipperError("yt-dlp returned invalid VOD metadata") from exc
    LOG.info("VOD duration: %.1f minutes", duration / 60)
    return duration


def count_buckets(messages: Iterable[dict], vod_duration: float) -> Counter[int]:
    """Stream aggregation: memory scales with VOD length, not comment count."""
    buckets: Counter[int] = Counter()
    counted = 0
    for message in messages:
        # Ignore moderation/events with no comment and malformed/pre-stream timestamps.
        if not message.get("message"):
            continue
        timestamp = message.get("time_in_seconds")
        if timestamp is None or isinstance(timestamp, bool):
            continue
        try:
            seconds = float(timestamp)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(seconds) or not 0 <= seconds < vod_duration:
            continue
        buckets[int(seconds // BUCKET_SECONDS)] += 1
        counted += 1
        if counted % 10000 == 0:
            LOG.info("Scanned %s chat comments...", f"{counted:,}")
    LOG.info("Counted %s comments across %s active 30-second buckets", f"{counted:,}", len(buckets))
    return buckets


def fetch_chat_buckets(url: str, vod_duration: float) -> Counter[int]:
    from chat_downloader import ChatDownloader

    LOG.info("Fetching full chat replay (large VODs may take several minutes)...")
    downloader = ChatDownloader()
    try:
        chat = downloader.get_chat(
            url, start_time=0, end_time=vod_duration,
            message_groups=["messages"], max_attempts=5, retry_timeout=3,
            interruptible_retry=False,
        )
        buckets = count_buckets(chat, vod_duration)
    except Exception as exc:
        raise ClipperError(
            "Chat replay could not be retrieved. Check that replay is enabled, "
            f"the VOD is public, and chat-downloader supports it. Details: {exc}"
        ) from exc
    finally:
        downloader.close()
    if not buckets:
        raise ClipperError("No timestamped chat comments found; no hype clips can be selected.")
    return buckets


def select_moments(buckets: Counter[int], count: int, duration: int,
                   vod_duration: float) -> list[Moment]:
    """Greedy highest-count-first selection; ties favor the earlier bucket."""
    selected: list[Moment] = []
    for bucket, comments in sorted(buckets.items(), key=lambda item: (-item[1], item[0])):
        if comments <= 0 or not 0 <= bucket * BUCKET_SECONDS < vod_duration:
            continue
        start = max(0, bucket * BUCKET_SECONDS - PRE_ROLL_SECONDS)
        end = min(vod_duration, start + duration)
        if end <= start or any(start < other.end and other.start < end for other in selected):
            continue
        selected.append(Moment(bucket, comments, start, end))
        if len(selected) == count:
            break
    return selected


def download_segment(url: str, moment: Moment, directory: Path) -> Path:
    # There is deliberately no whole-VOD download fallback.
    command = ytdlp_command() + [
        "--download-sections", f"*{moment.start:.3f}-{moment.end:.3f}",
        "--force-keyframes-at-cuts", "--downloader", "ffmpeg",
        "--format", "bv[height<=1080]+ba/b[height<=1080]/bv[height<=1080]",
        "--merge-output-format", "mkv", "--newline", "--no-simulate",
        "--output", str(directory / "raw.%(ext)s"), "--", url,
    ]
    run_command(command, "Segment download", max(900, int((moment.end - moment.start) * 20)))
    files = [path for path in directory.glob("raw.*")
             if path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".ts", ".flv"}
             and not re.search(r"\.f\d+\.", path.name)]
    if len(files) != 1 or files[0].stat().st_size == 0:
        raise ClipperError("yt-dlp did not produce exactly one complete segment")
    return files[0]


def probe_media(path: Path) -> dict:
    raw = run_command([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ], "Media inspection", 60, capture=True)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ClipperError("ffprobe returned invalid metadata") from exc


def convert_vertical(source: Path, destination: Path, duration: float) -> None:
    metadata = probe_media(source)
    streams = metadata.get("streams", [])
    if not any(stream.get("codec_type") == "video" for stream in streams):
        raise ClipperError("Downloaded segment contains no video")
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y", "-i", str(source)]
    if not has_audio:
        command += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    command += [
        "-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0",
        # Normalize non-square pixels before cropping; crop before upscaling to save memory.
        "-vf", "scale=trunc(iw*sar/2)*2:ih,setsar=1,"
        "crop=w='min(iw,ih*9/16)':h='min(ih,iw*16/9)',"
        "scale=1080:1920:flags=lanczos,setsar=1,fps=30,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-threads", "2",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-af", "apad", "-t", f"{duration:.3f}", "-shortest",
        "-map_metadata", "-1", "-movflags", "+faststart", str(destination),
    ]
    run_command(command, "Vertical video conversion", max(600, int(duration * 20)))
    result = probe_media(destination)
    video = next((s for s in result.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in result.get("streams", []) if s.get("codec_type") == "audio"), {})
    try:
        actual_duration = float(result.get("format", {}).get("duration", 0))
    except (TypeError, ValueError) as exc:
        raise ClipperError("Output duration is invalid") from exc
    if (video.get("width"), video.get("height"), video.get("codec_name"), audio.get("codec_name")) != (
            1080, 1920, "h264", "aac"):
        raise ClipperError("Output validation failed: expected 1080x1920 H.264/AAC")
    if not math.isfinite(actual_duration) or abs(actual_duration - duration) > 0.5:
        raise ClipperError(f"Output duration {actual_duration:.2f}s differs from requested {duration:.2f}s")


def create_clips(url: str, moments: list[Moment], output_dir: Path = OUTPUT_DIR) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    failures = 0
    for rank, moment in enumerate(moments, 1):
        LOG.info("Clip %d/%d: %.1f-%.1fs, %d comments in bucket %ds",
                 rank, len(moments), moment.start, moment.end, moment.comments,
                 moment.bucket * BUCKET_SECONDS)
        try:
            # Same filesystem allows atomic publication; failures leave no partial MP4s.
            with tempfile.TemporaryDirectory(prefix=".auto_clipper_", dir=output_dir) as temp:
                directory = Path(temp)
                source = download_segment(url, moment, directory)
                ready = directory / "vertical.mp4"
                convert_vertical(source, ready, moment.end - moment.start)
                destination = output_dir / f"{run_id}_clip_{rank:02d}_{int(moment.start):06d}s.mp4"
                ready.replace(destination)
                LOG.info("Saved %s", destination)
        except (ClipperError, OSError) as exc:
            failures += 1
            LOG.error("Clip %d failed: %s", rank, exc)
    LOG.info("Finished: %d/%d clips saved in %s", len(moments) - failures, len(moments), output_dir.resolve())
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        check_dependencies(args.url)
        duration = get_vod_duration(args.url)
        buckets = fetch_chat_buckets(args.url, duration)
        moments = select_moments(buckets, args.clips, args.duration, duration)
        if not moments:
            raise ClipperError("No usable hype moments were found")
        if len(moments) < args.clips:
            LOG.warning("Only %d non-overlapping moments are available (requested %d)", len(moments), args.clips)
        return create_clips(args.url, moments)
    except (ClipperError, OSError) as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.warning("Interrupted; completed clips are preserved")
        return 130


if __name__ == "__main__":
    sys.exit(main())
