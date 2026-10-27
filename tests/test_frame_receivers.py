"""Reading frames leaves nothing behind: each recv() waits on the frame and on
the session ending, and must not leave the second wait pending when the frame
wins (it did, ~75 tasks a second for a whole session)."""

import asyncio
import unittest

import numpy as np
from av import AudioFrame, VideoFrame

from simli.transports.base_transport import Run
from simli.transports.p2p_transport import AudioFrameReceiver, VideoFrameReceiver


class FakeTrack:
    def __init__(self, frame):
        self.frame = frame

    async def recv(self):
        await asyncio.sleep(0)
        return self.frame

    def stop(self):
        pass


def _pending() -> int:
    return sum(1 for task in asyncio.all_tasks() if not task.done())


class FrameReceivers(unittest.IsolatedAsyncioTestCase):
    async def assertNoTasksLeft(self, receiver):
        await asyncio.sleep(0)
        before = _pending()
        for _ in range(200):
            self.assertIsNotNone(await receiver.recv())
        # Let the cancelled waits finish being cancelled.
        await asyncio.sleep(0)
        self.assertLessEqual(_pending(), before)

    async def test_video_frames_leave_no_pending_tasks(self):
        frame = VideoFrame.from_ndarray(np.zeros((16, 16, 3), np.uint8), format="rgb24")
        await self.assertNoTasksLeft(VideoFrameReceiver(FakeTrack(frame), Run()))

    async def test_audio_frames_leave_no_pending_tasks(self):
        frame = AudioFrame.from_ndarray(np.zeros((1, 320), np.int16), format="s16", layout="mono")
        await self.assertNoTasksLeft(AudioFrameReceiver(FakeTrack(frame), Run()))

    async def test_a_session_ending_still_ends_a_waiting_recv(self):
        class SilentTrack(FakeTrack):
            async def recv(self):
                await asyncio.Event().wait()

        run = Run()
        receiver = VideoFrameReceiver(SilentTrack(None), run)
        pending = asyncio.create_task(receiver.recv())
        await asyncio.sleep(0)
        run.run = False
        self.assertIsNone(await asyncio.wait_for(pending, 1))


if __name__ == "__main__":
    unittest.main()
