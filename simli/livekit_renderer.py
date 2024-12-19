import asyncio
import time

from av.audio.frame import AudioFrame
from av.video.frame import VideoFrame
from . import SimliClient

try:
    from livekit import rtc

except ImportError as e:
    raise ImportError(
        "livekit is required for LivekitRenderer, Install optional dependencies using \n\"pip install 'simli-ai[livekit]'\""
    ) from e

FPS = 30


class LivekitRenderer:
    def __init__(
        self,
        simliClient: SimliClient,
        room_url: str,
        room_token: str,
        room: rtc.Room,
        width: int = 512,
        height: int = 512,
    ):
        self.client = simliClient
        self.room = room
        self.room_url = room_url
        self.room_token = room_token

        self.videoSource = rtc.VideoSource(width, height)
        self.audioSource = rtc.AudioSource(48000, 2, 20)
        self.videoTrack = rtc.LocalVideoTrack.create_video_track(
            "SimliVideo", self.videoSource
        )
        self.audioTrack = rtc.LocalAudioTrack.create_audio_track(
            "SimliAudio", self.audioSource
        )
        self.CameraOptions = rtc.TrackPublishOptions(
            source=rtc.TrackSource.SOURCE_CAMERA,
            simulcast=True,
            video_encoding=rtc.VideoEncoding(
                max_framerate=FPS,
                max_bitrate=3_000_000,
            ),
        )
        self.CameraOptions.source = rtc.TrackSource.SOURCE_CAMERA
        self.MicrophoneOptions = rtc.TrackPublishOptions()
        self.MicrophoneOptions.source = rtc.TrackSource.SOURCE_MICROPHONE
        self.lastTimestamp = 0

    async def InitLivekit(self):
        await self.room.connect(url=self.room_url, token=self.room_token)
        await self.room.local_participant.publish_track(
            self.videoTrack, self.CameraOptions
        )
        await self.room.local_participant.publish_track(
            self.audioTrack, self.MicrophoneOptions
        )
        print("Published Tracks")

    async def render(self):
        self.videoQueue: asyncio.Queue[VideoFrame] = asyncio.Queue()
        self.audioQueue: asyncio.Queue[AudioFrame] = asyncio.Queue()
        await asyncio.gather(
            self.PublishVideo(),
            self.PublishAudio(),
            self.PopulateVideoQueue(),
            self.PopulateAudioQueue(),
        )

    async def PopulateVideoQueue(self):
        async for frame in self.client.getVideoStreamIterator("yuva420p"):
            if frame is None:
                break
            await self.videoQueue.put(frame)
            print("Video Queue Size:", self.videoQueue.qsize())

    async def PopulateAudioQueue(self):
        async for frame in self.client.getAudioStreamIterator():
            if frame is None:
                break
            await self.audioQueue.put(frame)
            print("Audio Queue Size:", self.audioQueue.qsize())

    async def PublishVideo(self):
        next_frame_time = time.perf_counter()
        while (frame := await self.videoQueue.get()) is not None:
            if frame is None:
                break
            frameLivekit = rtc.VideoFrame(
                frame.width,
                frame.height,
                rtc.VideoBufferType.I420A,
                frame.to_ndarray().tobytes(),
            )
            self.videoSource.capture_frame(
                frameLivekit,
            )
            next_frame_time += 1 / FPS
            self.lastTimestamp = frame.time
            await asyncio.sleep(next_frame_time - time.perf_counter())

    async def PublishAudio(self):
        while (frame := await self.audioQueue.get()) is not None:
            frameMat = frame.to_ndarray()
            frame = rtc.AudioFrame(frameMat.tobytes(), 48000, 2, frameMat.shape[1] // 2)
            await self.audioSource.capture_frame(frame)
