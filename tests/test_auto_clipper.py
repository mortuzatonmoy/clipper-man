import argparse
from collections import Counter
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import auto_clipper as clipper

URL = "https://www.youtube.com/watch?v=abcdefghijk"


class SelectionTests(unittest.TestCase):
    def test_bucket_boundaries_and_invalid_messages(self):
        times = [0, 29.999, 30, "59.9", 60, -1, None, "bad", float("nan"), float("inf"), True, 90]
        messages = [{"message": "wow", "time_in_seconds": t} for t in times]
        messages += [{"time_in_seconds": 1}, {"message": "", "time_in_seconds": 1}]
        self.assertEqual(clipper.count_buckets(iter(messages), 90), Counter({0: 2, 1: 2, 2: 1}))

    def test_highest_first_and_ties(self):
        moments = clipper.select_moments(Counter({4: 90, 2: 100, 6: 90}), 3, 45, 300)
        self.assertEqual([m.bucket for m in moments], [2, 4, 6])

    def test_overlapping_neighbors_are_excluded(self):
        moments = clipper.select_moments(Counter({1: 90, 2: 100, 3: 80, 4: 70}), 5, 45, 300)
        self.assertEqual([m.bucket for m in moments], [2, 4])
        self.assertEqual((moments[0].start, moments[0].end), (50, 95))

    def test_touching_intervals_are_allowed(self):
        moments = clipper.select_moments(Counter({1: 10, 2: 9}), 2, 30, 100)
        self.assertEqual([(m.start, m.end) for m in moments], [(20, 50), (50, 80)])

    def test_clamp_start_and_end(self):
        moments = clipper.select_moments(Counter({0: 10, 3: 9}), 5, 45, 100)
        self.assertEqual([(m.start, m.end) for m in moments], [(0, 45), (80, 100)])

    def test_empty_chat(self):
        self.assertEqual(clipper.select_moments(Counter(), 5, 45, 100), [])

    def test_randomized_nonoverlap_and_bounds(self):
        rng = random.Random(7)
        for _ in range(100):
            vod_length = rng.randint(1, 10000)
            buckets = Counter({b: rng.randint(1, 1000) for b in range((vod_length + 29) // 30)})
            result = clipper.select_moments(buckets, 20, rng.randint(1, 200), vod_length)
            self.assertLessEqual(len(result), 20)
            for i, moment in enumerate(result):
                self.assertGreaterEqual(moment.start, 0)
                self.assertLess(moment.start, moment.end)
                self.assertLessEqual(moment.end, vod_length)
                for other in result[i + 1:]:
                    self.assertTrue(moment.end <= other.start or other.end <= moment.start)


class InputTests(unittest.TestCase):
    def test_defaults(self):
        args = clipper.parse_args(["--url", URL])
        self.assertEqual((args.clips, args.duration), (5, 45))

    def test_canonical_urls(self):
        for value in [URL + "&list=evil&t=60", "https://youtu.be/abcdefghijk?t=60",
                      "https://www.youtube.com/live/abcdefghijk"]:
            self.assertEqual(clipper.vod_url(value), URL)
        self.assertEqual(clipper.vod_url("https://twitch.tv/videos/123?t=20"),
                         "https://www.twitch.tv/videos/123")

    def test_reject_bad_urls(self):
        for value in ["file:///etc/passwd", "https://youtube.com.evil/watch?v=abcdefghijk",
                      "https://youtube.com@evil.test/", "--exec=evil", "https://twitch.tv/channel",
                      "https://www.youtube.com/playlist?list=123", "https://youtu.be/nope",
                      "https://youtube.com:8080/watch?v=abcdefghijk"]:
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                clipper.vod_url(value)

    def test_reject_bad_numbers(self):
        for value in ["0", "-1", "nan", "1.5", "$(touch bad)"]:
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                clipper.positive_int(value)


class PipelineTests(unittest.TestCase):
    def tearDown(self):
        clipper.COOKIES_FILE = None

    def test_cookies_passed_only_when_set(self):
        self.assertNotIn("--cookies", clipper.ytdlp_command())
        clipper.COOKIES_FILE = Path("c.txt")
        command = clipper.ytdlp_command()
        self.assertEqual(command[command.index("--cookies") + 1], "c.txt")

    def test_bot_check_hint_on_youtube_failure(self):
        with patch.object(clipper, "run_command", side_effect=clipper.ClipperError("failed")):
            with self.assertRaisesRegex(clipper.ClipperError, "YT_COOKIES"):
                clipper.get_vod_duration(URL)

    def test_live_and_unusable_metadata_rejected(self):
        for metadata in [{"is_live": True, "duration": 100}, {"live_status": "post_live"},
                         {"duration": None}, {"duration": float("nan")}, {}]:
            with patch.object(clipper, "run_command", return_value=json.dumps(metadata)):
                with self.assertRaises(clipper.ClipperError):
                    clipper.get_vod_duration(URL)

    def test_metadata_is_only_lookup(self):
        with patch.object(clipper, "run_command", return_value='{"duration": 100}') as run:
            self.assertEqual(clipper.get_vod_duration(URL), 100)
        self.assertIn("--skip-download", run.call_args.args[0])

    def test_range_download_and_no_full_vod_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "raw.mkv").write_bytes(b"media")
            with patch.object(clipper, "run_command") as run:
                self.assertEqual(clipper.download_segment(URL, clipper.Moment(2, 10, 50, 95), folder),
                                 folder / "raw.mkv")
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--download-sections") + 1], "*50.000-95.000")
            self.assertIn("--force-keyframes-at-cuts", command)
            fmt = command[command.index("--format") + 1]
            self.assertEqual(fmt, "bv*+ba/b")
            self.assertNotIn("height", fmt)
            with patch.object(clipper, "run_command", side_effect=clipper.ClipperError("failed")) as run:
                with self.assertRaises(clipper.ClipperError):
                    clipper.download_segment(URL, clipper.Moment(2, 10, 50, 95), folder)
                self.assertEqual(run.call_count, 1)

    def test_cleanup_partial_failure_and_preserve_success(self):
        def download(url, moment, folder):
            raw = folder / "raw.mkv"
            raw.write_bytes(b"raw")
            return raw

        calls = 0

        def convert(source, destination, duration):
            nonlocal calls
            calls += 1
            destination.write_bytes(b"partial" if calls == 1 else b"complete")
            if calls == 1:
                raise clipper.ClipperError("simulated encoder failure")

        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "previous.mp4").write_bytes(b"previous run")
            with patch.object(clipper, "download_segment", side_effect=download), \
                    patch.object(clipper, "encode_clip", side_effect=convert):
                status = clipper.create_clips(URL, [clipper.Moment(1, 10, 20, 50),
                                                   clipper.Moment(3, 9, 80, 110)], folder)
            self.assertEqual(status, 1)
            self.assertEqual(len(list(folder.glob("*.mp4"))), 2)
            self.assertEqual(list(folder.glob(".auto_clipper_*")), [])
            self.assertEqual((folder / "previous.mp4").read_bytes(), b"previous run")

    def test_interruption_cleans_raw_files(self):
        def interrupt(url, moment, folder):
            (folder / "raw.mkv").write_bytes(b"partial")
            raise KeyboardInterrupt
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(clipper, "download_segment", side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    clipper.create_clips(URL, [clipper.Moment(1, 1, 20, 50)], Path(temp))
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_subprocess_timeout_is_actionable(self):
        with self.assertRaisesRegex(clipper.ClipperError, "timed out"):
            clipper.run_command([sys.executable, "-c", "import time; time.sleep(60)"], "test", 1)

    def test_subprocess_failure_and_capture(self):
        with self.assertRaisesRegex(clipper.ClipperError, "exit 7"):
            clipper.run_command([sys.executable, "-c", "raise SystemExit(7)"], "test", 5)
        self.assertEqual(clipper.run_command([sys.executable, "-c", "print('ok')"],
                                            "test", 5, capture=True).strip(), "ok")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/ffprobe not installed")
class MediaIntegrationTests(unittest.TestCase):
    def test_dimensions_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for size, audio, sar in [("1920x1080", True, None), ("640x360", True, None),
                                     ("180x400", False, None), ("641x361", True, None),
                                     ("720x480", True, "32/27")]:
                with self.subTest(size=size, audio=audio, sar=sar):
                    source = folder / "source.mkv"
                    output = folder / "result.mp4"
                    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                           "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25"]
                    if audio:
                        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
                    if sar:
                        cmd += ["-vf", f"setsar={sar}"]
                    cmd += ["-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv444p"]
                    if audio:
                        cmd += ["-c:a", "aac"]
                    subprocess.run(cmd + [str(source)], check=True, capture_output=True)
                    clipper.encode_clip(source, output, 1.5)
                    src = clipper.video_geometry(clipper.probe_media(source))
                    out = clipper.video_geometry(clipper.probe_media(output))
                    self.assertEqual(src, out)
                    self.assertEqual(f"{out[0]}x{out[1]}", size)
                    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(output),
                                    "-f", "null", "-"], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
