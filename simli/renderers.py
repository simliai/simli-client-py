import asyncio

import av
import av.audio
import av.container
import av.packet
import av.video

from .simli import SimliClient


class NPArrayRenderer:
    pass


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
    ):
        self.client = client
        self.videoStream: av.video.VideoStream
        self.audioStream: av.audio.AudioStream
        self.container: av.container.OutputContainer
        self.filename = filename
        self.videoCodec = videoCodec
        self.audioCodec = audioCodec

    async def render(self):
        """
        Start rendering the video and audio stream to the file.
        """
        self.container = av.open(self.filename, "w")

        self.videoStream = self.container.add_stream(self.videoCodec, rate=30)
        self.videoStream.pix_fmt = "yuv420p"

        self.audioStream = self.container.add_stream(self.audioCodec)
        videoEncodeTask = asyncio.create_task(self.encodeVideo())
        audioEncodeTask = asyncio.create_task(self.encodeAudio())
        await asyncio.gather(videoEncodeTask, audioEncodeTask)
        # Close the file
        self.container.close()

    async def encodeVideo(self):
        async for frame in self.client.getVideoStreamIterator("yuva420p"):
            if frame is None:
                break
            self.videoStream.width = frame.width
            self.videoStream.height = frame.height
            for packet in self.videoStream.encode(frame):
                self.container.mux(packet)
        for packet in self.videoStream.encode():
            self.container.mux(packet)

    async def encodeAudio(self):
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
            import cv2
            import pyaudio  # type: ignore # noqa: F821
        except ImportError:
            raise ImportError(
                "cv2 and pyaudio are required for LocalRenderer, Install optional dependencies using \n\"pip install 'simli[local]'\""
            )

        self.client = client
        self.videoOutput = cv2.namedWindow(
            windowName, cv2.WINDOW_NORMAL | cv2.WINDOW_AUTOSIZE
        )
        cv2.resizeWindow(windowName, (512, 512))
        self.videoBuffer = []

        self.audioFormat = pyaudio.paInt16
        self.audioChannels = 2
        self.audioRate = 48000
        self.pyaudio = pyaudio.PyAudio()
        self.audioOutput = self.pyaudio.open(
            format=self.audioFormat,
            channels=self.audioChannels,
            rate=self.audioRate,
            output=True,
            frames_per_buffer=1024,
        )
        self.audioBuffer = []

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
                cv2.destroyAllWindows()  # type: ignore # noqa: F821
                break
            self.videoBuffer.append(frame.to_ndarray())
            cv2.imshow("Simli", cv2.cvtColor(self.videoBuffer[0], cv2.COLOR_RGB2BGR))  # type: ignore # noqa: F821
            self.videoBuffer.pop(0)
            cv2.waitKey(1)  # type: ignore # noqa: F821

    async def playAudio(self):
        async for frame in self.client.getAudioStreamIterator():
            if frame is None:
                break
            self.audioBuffer.append(frame.to_ndarray())
            self.audioOutput.write(self.audioBuffer[0].tobytes())
            self.audioBuffer.pop(0)
