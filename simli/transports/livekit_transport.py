import asyncio
import fractions
import json
import urllib.parse as urlparse
from collections.abc import AsyncGenerator, AsyncIterator
from urllib.parse import urlencode

import numpy as np
from av import AudioFrame, VideoFrame
from httpx import AsyncClient

try:
    from livekit import rtc
except ImportError:
    raise RuntimeError(
        'livekit not installed, please uv add "simli-ai[livekit]" or pip install "simli-ai[livekit]" to use livekit transport for simli'
    )

from ..critical_exceptions import SimliConnectionError, SimliError
from ..events import SimliEvent
from ..logger import logger
from .base_transport import BaseTransport, Run, connectWebsocket, createSessionToken

LK_TO_AV_FORMAT = {
    0: "rgba",
    1: "abgr",
    2: "argb",
    3: "bgra",
    4: "rgb24",
    5: "yuv420p",
    6: "yuva420",
    7: "yuv422",
    8: "yuv444",
    9: "yuv420p10le",
    10: "nv12",
}

MICROSECOND_TO_90KHZ_FACTOR = 0.09


class VideoFrameReceiver:
    kind = "video"

    def __init__(
        self,
        source: rtc.VideoStream,
        run: Run,
        fps: float,
        width: int,
        height: int,
    ):
        self.source = source
        self.first = True
        self.frameCount = 0
        self.run = run
        self.fps = fps
        self.width = width
        self.height = height
        self.firstStamp: int = 0

    async def recv(self) -> VideoFrame | None:
        if not self.run.run:
            return None
        try:
            frame = None
            frameTask = asyncio.create_task(self.source.__anext__())
            waitTask = asyncio.create_task(self.run.event.wait())
            await asyncio.wait(
                [
                    frameTask,
                    waitTask,
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not self.run.run:
                frameTask.cancel()
                await waitTask
                return None
            # The frame won: without this, every frame left a waiter pending
            # until the session ended (~75 a second).
            waitTask.cancel()

            lkFrame: rtc.VideoFrameEvent = await frameTask
            if not isinstance(lkFrame, rtc.VideoFrameEvent):
                raise RuntimeError()
            if self.firstStamp == 0:
                self.firstStamp = lkFrame.timestamp_us
            frame = VideoFrame.from_numpy_buffer(
                np.frombuffer(lkFrame.frame.data, dtype=np.uint8).reshape(
                    lkFrame.frame.height, lkFrame.frame.width, 3
                ),
                format=LK_TO_AV_FORMAT[lkFrame.frame.type],
            )
            frame = frame.reformat(self.width, self.height, format="yuv420p")
            frame.time_base = fractions.Fraction(1, 90_000)
            frame.pts = int(
                (lkFrame.timestamp_us - self.firstStamp) * (MICROSECOND_TO_90KHZ_FACTOR)
            )
            self.frameCount += 1
            return frame
        except StopAsyncIteration:
            self.done = True
            return None
        except Exception:
            self.done = True
            logger.exception("Receiving a LiveKit video frame failed")
            return None


class AudioFrameReceiver:
    kind = "audio"

    def __init__(
        self,
        source: rtc.AudioStream,
        run: Run,
    ):
        super().__init__()
        self.source = source
        self.run = run
        self.sampleCount: int = 0
        self.done = False

    async def recv(self) -> AudioFrame | None:
        if not self.run.run:
            return None
        try:
            frameTask = asyncio.create_task(self.source.__anext__())
            waitTask = asyncio.create_task(self.run.event.wait())
            await asyncio.wait(
                [
                    frameTask,
                    waitTask,
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not self.run.run:
                frameTask.cancel()
                await waitTask
                return None
            # The frame won: without this, every frame left a waiter pending
            # until the session ended (~75 a second).
            waitTask.cancel()
            lkFrame = await frameTask
            if not isinstance(lkFrame, rtc.AudioFrameEvent):
                raise RuntimeError()
            frame = AudioFrame.from_ndarray(
                np.frombuffer(
                    lkFrame.frame.data,
                    dtype=np.int16,
                ).reshape(1, -1),
                layout="stereo" if lkFrame.frame.num_channels == 2 else "mono",
            )
            frame.sample_rate = lkFrame.frame.sample_rate
            frame.time_base = fractions.Fraction(1, lkFrame.frame.sample_rate)
            frame.pts = self.sampleCount
            self.sampleCount += frame.samples
            return frame
        except StopAsyncIteration:
            return None
        except Exception:
            logger.exception("Receiving a LiveKit audio frame failed")
            return None


class LivekitTransport(BaseTransport):
    audioReceiver: AudioFrameReceiver
    videoReceiver: VideoFrameReceiver
    receiverTask: asyncio.Task
    joinInfoReceived: asyncio.Event

    def __init__(
        self,
        run: Run,
        api_key: str,
        config: dict,
        simli_ws_url: str,
        simli_http_url: str,
    ):
        super().__init__(run)
        self.simliWSURL = simli_ws_url
        self.simliHTTPURL = simli_http_url
        self.config = config
        self.api_key = api_key
        self.joinInfoReceived = asyncio.Event()
        self.videoInfoReceived = asyncio.Event()
        self.cached_session_token = ""

    async def Connect(self) -> None:
        async with AsyncClient() as client:
            self.cached_session_token = await createSessionToken(
                client, self.simliHTTPURL, self.api_key, self.config
            )

        url_parts = list(urlparse.urlparse(f"{self.simliWSURL}/compose/webrtc/livekit"))
        query = dict(urlparse.parse_qsl(url_parts[4]))
        query.update(
            {
                "session_token": self.cached_session_token,
            }
        )
        url_parts[4] = urlencode(query)

        self.wsConnection = await connectWebsocket(urlparse.urlunparse(url_parts))

        self.RegisterEvent(SimliEvent.CONNECTION_INFO, self.AwaitAnswer)
        self.RegisterEvent(SimliEvent.VIDEO_META, self.RegisterVideoInfo)
        self.RegisterEvent(SimliEvent.STOP, self.stop_callback)
        self.RegisterEvent(SimliEvent.ERROR, self.stop_callback)
        self.receiverTask = asyncio.create_task(self.handleMessages())
        self.LocalPeer = rtc.Room()
        self.LocalPeer.on("track_subscribed", self.registerTrack)
        await self.waitFor(self.joinInfoReceived, "Getting the LiveKit room from Simli")
        try:
            livekitURL = self.sessionJoinInfo["livekit_url"]
            livekitToken = self.sessionJoinInfo["livekit_token"]
        except KeyError as e:
            raise SimliError(f"Simli sent invalid LiveKit join info, missing {e}") from e
        try:
            await self.LocalPeer.connect(livekitURL, livekitToken)
        except rtc.ConnectError as e:
            raise SimliConnectionError(f"Joining the LiveKit room failed: {e.message}") from e
        await self.waitFor(self.started, "Starting the Simli session")
        if not self.run.run:
            raise self.endedError("Starting the Simli session")

    async def AwaitAnswer(self, joinInfoJSON: str) -> None:
        try:
            self.sessionJoinInfo = json.loads(joinInfoJSON)
        except ValueError:
            await self.unknownMessage(joinInfoJSON)
            return
        self.joinInfoReceived.set()

    async def RegisterVideoInfo(self, VideoInfoJSON: str) -> None:
        try:
            parsedMessage = json.loads(VideoInfoJSON)
            self.fps = parsedMessage["video_metadata"]["fps"]
            self.width = parsedMessage["video_metadata"]["width"]
            self.height = parsedMessage["video_metadata"]["height"]
        except (ValueError, KeyError, TypeError):
            await self.unknownMessage(VideoInfoJSON)
            return
        self.videoInfoReceived.set()

    def AudioFrameIterator(self) -> AsyncIterator[AudioFrame]:
        return self._AudioFrameGenerator()

    def VideoFrameIterator(self) -> AsyncIterator[VideoFrame]:
        return self._VideoFrameGenerator()

    async def _AudioFrameGenerator(self) -> AsyncGenerator[AudioFrame, None]:
        while self.run.run and not hasattr(self, "audioReceiver"):
            await asyncio.sleep(0.001)
        while self.run.run:
            frame = await self.audioReceiver.recv()
            if frame is None:
                self.run.run = False
                break
            yield frame

    async def _VideoFrameGenerator(self) -> AsyncGenerator[VideoFrame, None]:
        while self.run.run and not hasattr(self, "videoReceiver"):
            await asyncio.sleep(0.001)
        while self.run.run:
            frame = await self.videoReceiver.recv()
            if frame is None:
                self.run.run = False
                break
            yield frame

    async def stop(self) -> None:
        self.run.run = False
        if self.stopped:
            return
        self.stopped = True
        try:
            await self.LocalPeer.disconnect()
        except Exception:
            pass
        try:
            await self.wsConnection.send(b"DONE")
            await self.wsConnection.close()
        except Exception:
            pass
        logger.info("Stopping Simli Connection")

    def registerTrack(
        self,
        track: rtc.RemoteTrack,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ):
        # Called on the event loop, which must keep running for the video metadata to arrive
        asyncio.ensure_future(self.createReceiver(track))

    async def createReceiver(self, track: rtc.RemoteTrack) -> None:
        try:
            await self.waitFor(self.videoInfoReceived, "Getting the video metadata from Simli")
        except SimliError:
            return
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            receiver = AudioFrameReceiver(
                rtc.AudioStream(
                    track,
                    sample_rate=48000,
                    num_channels=2,
                ),
                self.run,
            )
            self.audioReceiver = receiver
        elif track.kind == rtc.TrackKind.KIND_VIDEO:
            receiver = VideoFrameReceiver(
                rtc.VideoStream(track, format=rtc.VideoBufferType.RGB24),
                self.run,
                self.fps,
                self.width,
                self.height,
            )
            self.videoReceiver = receiver
