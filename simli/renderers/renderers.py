import asyncio
import fractions

import av
import av.audio
import av.container
import av.packet
import av.video

from ..simli import SimliClient

# The video's time base in FileRenderer's file: a millisecond, fine enough that every frame keeps
# its own timing, whatever the frame rate
VIDEO_TIME_BASE = fractions.Fraction(1, 1000)


def videoPts(frame: av.VideoFrame, lastPts: int | None) -> int:
    """
    frame's pts in VIDEO_TIME_BASE, after lastPts (the previous frame's, None for the first).

    The session's video comes with WebRTC's 90 kHz timestamps, which jitter: a frame can arrive
    stamped as early as, or earlier than, the one before it. The muxer refuses a pts that doesn't
    increase, so such a frame goes one tick after the last instead. A frame without a timestamp
    follows the last one.
    """
    if frame.pts is None or frame.time_base is None:
        return 0 if lastPts is None else lastPts + 1
    pts = round(frame.pts * frame.time_base / VIDEO_TIME_BASE)
    if lastPts is not None and pts <= lastPts:
        return lastPts + 1
    return pts


class NDArrayRenderer:
    def __init__(self, client: SimliClient):
        import numpy as np

        self.client = client
        self.videoBuffer: list[np.ndarray] = []
        self.audioBuffer: list[np.ndarray] = []
        self.video: np.ndarray | None = None
        self.audio: np.ndarray | None = None
        self.np = np

    async def render(self):
        videoEncodeTask = asyncio.create_task(self.getVideo())
        audioEncodeTask = asyncio.create_task(self.getAudio())
        await asyncio.gather(videoEncodeTask, audioEncodeTask)
        self.video = self.np.array(self.videoBuffer)
        self.audio = self.np.concat(self.audioBuffer, axis=1)
        return self.video, self.audio

    async def getVideo(self):
        async for frame in self.client.getVideoStreamIterator("rgb24"):
            if frame is None:
                break
            self.videoBuffer.append(frame.to_ndarray())

    async def getAudio(self):
        async for frame in self.client.getAudioStreamIterator():
            if frame is None:
                break

            self.audioBuffer.append(frame.to_ndarray())


class FileRenderer:
    """
    Dumps the video and audio stream from a :class:`SimliClient` to a file.
    """

    def __init__(
        self,
        client: SimliClient,
        filename: str = "output.mp4",
        videoCodec: str = "h264",
        audioCodec: str = "aac",
        outputFormat: str | None = "mp4",
    ):
        self.client = client
        self.videoStream: av.video.VideoStream
        self.audioStream: av.audio.AudioStream
        self.container: av.container.OutputContainer
        self.filename = filename
        self.videoCodec = videoCodec
        self.audioCodec = audioCodec
        self.outputFormat = outputFormat
        self.syncLock = False

    async def render(self):
        """
        Start rendering the video and audio stream to the file.
        """
        self.container = av.open(self.filename, "w", format=self.outputFormat)

        videoStream = self.container.add_stream(self.videoCodec, rate=25)
        if not isinstance(videoStream, av.VideoStream):
            raise RuntimeError()
        self.videoStream = videoStream
        # Frames are encoded at their own timestamps (videoPts), not at 1/25 s steps: rounded to
        # 1/25 s, jittered timestamps collided and the muxer failed
        self.videoStream.codec_context.time_base = VIDEO_TIME_BASE
        self.videoStream.pix_fmt = "yuv420p"
        self.videoStream.width = 768
        self.videoStream.height = 768

        audioStream = self.container.add_stream(self.audioCodec)
        if not isinstance(audioStream, av.AudioStream):
            raise RuntimeError()
        self.audioStream = audioStream
        videoEncodeTask = asyncio.create_task(self.encodeVideo())
        audioEncodeTask = asyncio.create_task(self.encodeAudio())
        await asyncio.gather(videoEncodeTask, audioEncodeTask)
        # Close the file
        self.container.close()

    async def encodeVideo(self):
        firstFrame = True
        lastPts: int | None = None
        async for frame in self.client.getVideoStreamIterator("rgb24"):
            if firstFrame:
                firstFrame = False
                self.syncLock = True
            if frame is None:
                break
            lastPts = frame.pts = videoPts(frame, lastPts)
            frame.time_base = VIDEO_TIME_BASE
            self.videoStream.width = frame.width
            self.videoStream.height = frame.height
            for packet in self.videoStream.encode(frame):
                self.container.mux(packet)
        for packet in self.videoStream.encode():
            self.container.mux(packet)

    async def encodeAudio(self):
        while not self.syncLock:
            await asyncio.sleep(0.001)

        async for frame in self.client.getAudioStreamIterator():
            if frame is None:
                break
            for packet in self.audioStream.encode(frame):
                self.container.mux(packet)
        for packet in self.audioStream.encode():
            self.container.mux(packet)


class LocalRenderer:
    """
    Outputs the video and audio steram to local display and speaker respectively. Can not be used in a headless environment. Uses OpenCV for video and PyAudio for audio.
    """

    def __init__(self, client: SimliClient, windowName: str = "Simli"):
        try:
            import cv2  # ty:ignore[unresolved-import]
            import pyaudio  # ty:ignore[unresolved-import]
        except ImportError:
            raise ImportError(
                "cv2 and pyaudio are required for LocalRenderer, Install optional dependencies using \n\"pip install 'simli-ai[local]'\""
            )

        self.client = client
        self.videoOutput = cv2.namedWindow(windowName, cv2.WINDOW_NORMAL | cv2.WINDOW_AUTOSIZE)
        cv2.resizeWindow(windowName, (512, 512))
        self.videoBuffer = []

        self.audioFormat = pyaudio.paInt16
        self.audioChannels = 2
        self.audioRate = 48000
        self.pyaudioInstance = pyaudio.PyAudio()
        self.audioOutput = self.pyaudioInstance.open(
            format=self.audioFormat,
            channels=self.audioChannels,
            rate=self.audioRate,
            output=True,
            frames_per_buffer=1024,
        )
        self.audioBuffer = []
        self.cv2 = cv2
        self.pyaudio = pyaudio

    async def render(self):
        """
        Start displaying the video
        """
        videoTask = asyncio.create_task(self.displayVideo())
        audioTask = asyncio.create_task(self.playAudio())
        await asyncio.gather(videoTask, audioTask)

    async def displayVideo(self):
        async for frame in self.client.getVideoStreamIterator("rgb24"):
            if frame is None:
                self.cv2.destroyAllWindows()
                break
            self.videoBuffer.append(frame.to_ndarray())
            self.cv2.imshow(
                "Simli",
                self.cv2.cvtColor(self.videoBuffer[0], self.cv2.COLOR_RGB2BGR),
            )
            self.videoBuffer.pop(0)
            self.cv2.waitKey(1)

    async def playAudio(self):
        async for frame in self.client.getAudioStreamIterator():
            if frame is None:
                self.audioOutput.close()
                break
            self.audioBuffer.append(frame.to_ndarray())
            self.audioOutput.write(self.audioBuffer[0].tobytes())
            self.audioBuffer.pop(0)


__all__ = [
    "NDArrayRenderer",
    "FileRenderer",
    "LocalRenderer",
]
