import importlib.util
import io
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "rtsp_publisher.py"
spec = importlib.util.spec_from_file_location("rtsp_publisher", MODULE_PATH)
rtsp_publisher = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rtsp_publisher
assert spec.loader is not None
spec.loader.exec_module(rtsp_publisher)


class CameraPlaybackStateTests(unittest.TestCase):
    def clip(self, name: str):
        return rtsp_publisher.make_clip_ref(Path(name))

    def test_seed_queues_only_newest_existing_clip(self):
        clips = [
            self.clip("feeder-2026-03-10T10-00-00+00-00.mp4"),
            self.clip("feeder-2026-03-10T10-01-00+00-00.mp4"),
            self.clip("feeder-2026-03-10T10-02-00+00-00.mp4"),
        ]
        state = rtsp_publisher.CameraPlaybackState()

        seeded = state.seed_from_existing(clips)

        self.assertIsNotNone(seeded)
        self.assertEqual(seeded.name, "feeder-2026-03-10T10-02-00+00-00.mp4")
        self.assertEqual([clip.name for clip in state.pending_clips], [seeded.name])
        self.assertEqual(state.discovered_highwater, seeded.sort_key)

    def test_older_remaining_files_do_not_requeue_after_latest_clip_completed(self):
        older = self.clip("feeder-2026-03-10T10-00-00+00-00.mp4")
        newer = self.clip("feeder-2026-03-10T10-01-00+00-00.mp4")
        state = rtsp_publisher.CameraPlaybackState()
        state.seed_from_existing([older, newer])

        preparing = state.begin_prepare()
        self.assertEqual(preparing, newer)
        state.mark_playing(preparing)
        state.mark_holding(preparing)

        discovered = state.discover_new_clips([older])

        self.assertEqual(discovered, [])
        self.assertEqual(list(state.pending_clips), [])
        self.assertEqual(state.last_completed_clip, newer)
        self.assertEqual(state.held_clip, newer)

    def test_newer_discoveries_queue_once_in_order(self):
        clip1 = self.clip("feeder-2026-03-10T10-00-00+00-00.mp4")
        clip2 = self.clip("feeder-2026-03-10T10-01-00+00-00.mp4")
        clip3 = self.clip("feeder-2026-03-10T10-02-00+00-00.mp4")
        state = rtsp_publisher.CameraPlaybackState()
        state.seed_from_existing([clip1])

        first = state.begin_prepare()
        state.mark_playing(first)
        state.mark_holding(first)

        discovered = state.discover_new_clips([clip1, clip2, clip3])
        duplicate_discovery = state.discover_new_clips([clip1, clip2, clip3])

        self.assertEqual([clip.name for clip in discovered], [clip2.name, clip3.name])
        self.assertEqual([clip.name for clip in state.pending_clips], [clip2.name, clip3.name])
        self.assertEqual(duplicate_discovery, [])


class SilenceAndGeometryTests(unittest.TestCase):
    def test_build_silence_chunk_is_zeroed_pcm(self):
        silence = rtsp_publisher.build_silence_chunk(chunk_ms=125)
        expected_len = (rtsp_publisher.AUDIO_SAMPLE_RATE * rtsp_publisher.AUDIO_CHANNELS * rtsp_publisher.PCM_BYTES_PER_SAMPLE * 125) // 1000

        self.assertEqual(len(silence), expected_len)
        self.assertTrue(silence)
        self.assertEqual(set(silence), {0})

    def test_video_geometry_raw_frame_size_matches_yuv420p(self):
        geometry = rtsp_publisher.VideoGeometry(width=640, height=480)
        self.assertEqual(geometry.raw_frame_size, 640 * 480 * 3 // 2)
        self.assertEqual(geometry.size_arg, "640x480")


class PumpingAndBootstrapTests(unittest.TestCase):
    class FakeTarget:
        def __init__(self):
            self.writes = []

        def write(self, data: bytes, stop_event=None) -> None:
            self.writes.append(data)

    def clip(self, name: str):
        return rtsp_publisher.make_clip_ref(Path(name))

    def prepared_clip(self, *, has_audio: bool = True):
        clip = self.clip("bird-feeder-2026-03-10T20-54-50-156297+00-00.mp4")
        geometry = rtsp_publisher.VideoGeometry(width=1280, height=720)
        return rtsp_publisher.PreparedClip(
            clip=clip,
            probe=rtsp_publisher.ClipProbe(has_video=True, has_audio=has_audio, width=1280, height=720),
            geometry=geometry,
            still_frame=Path("/tmp/last-frame.yuv"),
        )

    def make_stream(self):
        return types.SimpleNamespace(
            camera="bird-feeder",
            audio_server=object(),
            video_server=object(),
            rtsp_url="rtsp://127.0.0.1:18554/repro/bird_feeder",
        )

    def test_video_pump_drops_incomplete_tail_chunk(self):
        src = io.BytesIO(b"frame000frame111tail")
        target = self.FakeTarget()

        rtsp_publisher._pump_stream_to_socket(
            src,
            target,
            threading.Event(),
            "video-test",
            chunk_size=8,
            require_full_chunk=True,
        )

        self.assertEqual(target.writes, [b"frame000", b"frame111"])

    def test_activate_prepared_clip_starts_video_and_audio_before_publisher(self):
        stream = self.make_stream()
        prepared = self.prepared_clip(has_audio=True)
        events = []

        with (
            mock.patch.object(rtsp_publisher, "start_clip_video_feeder", side_effect=lambda **kwargs: events.append("video") or "video-proc"),
            mock.patch.object(rtsp_publisher, "start_clip_audio_feeder", side_effect=lambda **kwargs: events.append("audio") or "audio-proc"),
            mock.patch.object(rtsp_publisher, "ensure_publisher", side_effect=lambda *args, **kwargs: events.append("publisher") or None),
        ):
            result = rtsp_publisher.activate_prepared_clip(
                stream,
                prepared,
                transport="tcp",
                video_fps=15,
                h264_preset="veryfast",
                h264_crf="23",
            )

        self.assertEqual(events, ["video", "audio", "publisher"])
        self.assertEqual(result[0], True)
        self.assertEqual(result[1], "audio-proc")
        self.assertEqual(result[2], "video-proc")

    def test_activate_prepared_clip_cleans_up_bootstrap_sources_on_failure(self):
        stream = self.make_stream()
        prepared = self.prepared_clip(has_audio=True)
        events = []

        with (
            mock.patch.object(rtsp_publisher, "start_clip_video_feeder", side_effect=lambda **kwargs: events.append("video") or "video-proc"),
            mock.patch.object(rtsp_publisher, "start_clip_audio_feeder", side_effect=lambda **kwargs: events.append("audio") or "audio-proc"),
            mock.patch.object(rtsp_publisher, "ensure_publisher", side_effect=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))),
            mock.patch.object(rtsp_publisher, "stop_pumped_proc", side_effect=lambda proc: events.append(f"stop:{proc}")),
            mock.patch.object(rtsp_publisher, "stop_threads", side_effect=lambda stop_event, threads: events.append("stop-threads")),
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                rtsp_publisher.activate_prepared_clip(
                    stream,
                    prepared,
                    transport="tcp",
                    video_fps=15,
                    h264_preset="veryfast",
                    h264_crf="23",
                )

        self.assertEqual(events, ["video", "audio", "stop:video-proc", "stop:audio-proc", "stop-threads"])


if __name__ == "__main__":
    unittest.main()
