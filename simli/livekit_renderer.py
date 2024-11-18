import asyncio
import time
from . import SimliClient

try:
    from livekit import rtc

except ImportError:
    raise ImportError(
        "livekit is required for LivekitRenderer, Install optional dependencies using \n\"pip install 'simli-ai[livekit]'\""
    )


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
        self.CameraOptions = rtc.TrackPublishOptions()
        self.CameraOptions.source = rtc.TrackSource.SOURCE_CAMERA
        self.MicrophoneOptions = rtc.TrackPublishOptions()
        self.MicrophoneOptions.source = rtc.TrackSource.SOURCE_MICROPHONE

    async def render(self):
        await self.room.connect(url=self.room_url, token=self.room_token)
        await self.room.local_participant.publish_track(
            self.videoTrack, self.CameraOptions
        )
        await self.room.local_participant.publish_track(
            self.audioTrack, self.MicrophoneOptions
        )
        print("Published Tracks")
        await asyncio.gather(self.PublishVideo(), self.PublishAudio())

    async def PublishVideo(self):
        s = time.time()
        async for frame in self.client.getVideoStreamIterator("yuva420p"):
            if frame is None:
                break
            frameLivekit = rtc.VideoFrame(
                frame.width,
                frame.height,
                rtc.VideoBufferType.I420,
                frame.to_ndarray().tobytes(),
            )

            self.videoSource.capture_frame(
                frameLivekit, timestamp_us=int(frame.time * (10**6))
            )
            await asyncio.sleep(1 / 30 - (time.time() - s))
            s = time.time()

    async def PublishAudio(self):
        async for frame in self.client.getAudioStreamIterator():
            if frame is None:
                break
            frame = rtc.AudioFrame(frame.to_ndarray().tobytes(), 48000, 2, 960)
            await self.audioSource.capture_frame(frame)
