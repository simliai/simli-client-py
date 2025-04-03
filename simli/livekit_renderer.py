import asyncio
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
        self.width = width
        self.height = height
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
                max_bitrate=1_500_000,
            ),
        )
        self.MicrophoneOptions = rtc.TrackPublishOptions()
        self.MicrophoneOptions.source = rtc.TrackSource.SOURCE_MICROPHONE
        self.lastTimestamp = 0
        self.avSynchronizer = rtc.AVSynchronizer(
            audio_source=self.audioSource,
            video_source=self.videoSource,
            video_fps=FPS,
            video_queue_size_ms=100,
        )
        self.videoSource.capture_frame(
            rtc.VideoFrame(
                self.width,
                self.height,
                rtc.VideoBufferType.I420A,
                (0).to_bytes(768 * 512, "little"),
            )
        )

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
            self.PopulateVideoQueue(),
            self.PopulateAudioQueue(),
        )

    async def PopulateVideoQueue(self):
        async for frame in self.client.getVideoStreamIterator("yuva420p"):
            if frame is None:
                print("Video Queue Empty")
                break
            frameLivekit = rtc.VideoFrame(
                frame.width,
                frame.height,
                rtc.VideoBufferType.I420A,
                frame.to_ndarray().tobytes(),
            )
            await self.avSynchronizer.push(frameLivekit)

    async def PopulateAudioQueue(self):
        async for frame in self.client.getAudioStreamIterator():
            if frame is None:
                break
            frame = frame.to_ndarray()
            await self.avSynchronizer.push(
                rtc.AudioFrame(frame.tobytes(), 48000, 2, frame.shape[1] // 2)
            )
