"""FileRenderer muxes a session's video whatever its timestamps do: WebRTC's 90 kHz timestamps
jitter, and rounded to the file's time base two frames could land on the same pts, or a late one
before the last, which the muxer refused ("non monotonically increasing dts") a few seconds in."""

import asyncio
import fractions
import os
import random
import tempfile
import unittest
from types import SimpleNamespace

import av
import numpy as np

from simli.renderers.renderers import VIDEO_TIME_BASE, FileRenderer, videoPts

RTP_VIDEO_TIME_BASE = fractions.Fraction(1, 90_000)
FRAME_TICKS = 3600  # 25 fps at 90 kHz


def jitteredPts(count: int, seed: int = 1) -> list[int]:
    """90 kHz timestamps 25 a second, each up to ~19 ms early or late, with a repeat and a
    step back thrown in"""
    rng = random.Random(seed)
    stamps = [max(0, i * FRAME_TICKS + rng.randint(-1700, 1700)) for i in range(count)]
    stamps[50] = stamps[49]
    stamps[120] = stamps[119] - 900
    return stamps


class FakeClient:
    """The two iterators FileRenderer reads, from canned frames"""

    def __init__(self, videoStamps: list[int], audioSeconds: float):
        self.videoStamps = videoStamps
        self.audioFrames = int(audioSeconds * 50)

    async def getVideoStreamIterator(self, targetFormat: str = "rgb24"):
        image = np.zeros((128, 128, 3), np.uint8)
        for i, pts in enumerate(self.videoStamps):
            image[:] = i % 256
            frame = av.VideoFrame.from_ndarray(image, format=targetFormat)
            frame.pts = pts
            frame.time_base = RTP_VIDEO_TIME_BASE
            yield frame
            await asyncio.sleep(0)

    async def getAudioStreamIterator(self, targetSampleRate: int = 48000):
        samples = np.zeros((1, 2 * 960), np.int16)
        for i in range(self.audioFrames):
            frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="stereo")
            frame.sample_rate = 48000
            frame.pts = i * 960
            frame.time_base = fractions.Fraction(1, 48000)
            yield frame
            await asyncio.sleep(0)


class VideoPts(unittest.TestCase):
    def frame(self, pts: int | None, timeBase=RTP_VIDEO_TIME_BASE):
        return SimpleNamespace(pts=pts, time_base=timeBase)

    def test_rescales_to_the_file_time_base(self):
        self.assertEqual(videoPts(self.frame(3600), None), 40)
        self.assertEqual(videoPts(self.frame(3690), 0), 41)  # 41 ms

    def test_never_repeats_or_goes_back(self):
        self.assertEqual(videoPts(self.frame(3600), 40), 41)
        self.assertEqual(videoPts(self.frame(1800), 40), 41)

    def test_frame_without_a_timestamp_follows_the_last(self):
        self.assertEqual(videoPts(self.frame(None, None), None), 0)
        self.assertEqual(videoPts(self.frame(None, None), 7), 8)

    def test_strictly_increasing_for_jittered_stamps(self):
        last = None
        for pts in jitteredPts(500):
            new = videoPts(self.frame(pts), last)
            if last is not None:
                self.assertGreater(new, last)
            last = new


class FileRendererMuxing(unittest.IsolatedAsyncioTestCase):
    async def test_jittered_video_muxes_into_a_playable_file(self):
        stamps = jitteredPts(300)  # 12 s, past the ~9 s the muxer used to fail at
        with tempfile.TemporaryDirectory() as directory:
            filename = os.path.join(directory, "output.mp4")
            await FileRenderer(FakeClient(stamps, audioSeconds=12), filename=filename).render()

            with av.open(filename) as container:
                video = container.streams.video[0]
                # Read while the file is open: a closed container's stream has no time base
                timeBase = video.time_base
                frames = [frame.pts for frame in container.decode(video)]
            with av.open(filename) as container:
                audioSamples = sum(frame.samples for frame in container.decode(audio=0))

        self.assertEqual(len(frames), len(stamps))
        self.assertEqual(sorted(frames), frames)
        self.assertEqual(len(set(frames)), len(frames))
        # The frames keep their own timing: the last one is where its timestamp put it
        lastSeconds = float(frames[-1] * timeBase)
        self.assertAlmostEqual(lastSeconds, stamps[-1] / 90_000, delta=0.05)
        self.assertAlmostEqual(audioSamples / 48000, 12, delta=0.1)

    def test_file_time_base_is_fine_enough_for_any_frame_rate(self):
        # A millisecond: frames at 60 fps are still ~17 ticks apart
        self.assertEqual(VIDEO_TIME_BASE, fractions.Fraction(1, 1000))


if __name__ == "__main__":
    unittest.main()
