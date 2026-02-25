import time
import asyncio
import fractions
import json
import traceback
import urllib.parse as urlparse
from collections.abc import AsyncIterator
from typing import AsyncGenerator
from urllib.parse import urlencode

import numpy as np
import websockets.asyncio.client
from av import AudioFrame, VideoFrame
from httpx import AsyncClient

try:
    from livekit import rtc
except ImportError:
    raise RuntimeError(
        'livekit not installed, please uv add "simli-ai[livekit]" or pip install "simli-ai[livekit]" to use livekit transport for simli'
    )

from ..critical_exceptions import SimliExceptions
from ..events import SimliEvent
from ..logger import logger
from .base_transport import BaseTransport, Run

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

            lkFrame: rtc.VideoFrameEvent = await frameTask
            if not isinstance(lkFrame, rtc.VideoFrameEvent):
                raise RuntimeError()
            if self.firstStamp == 0:
                self.firstStamp = lkFrame.timestamp_us
            frame = VideoFrame.from_numpy_buffer(
                np.frombuffer(lkFrame.frame.data, dtype=np.uint8).reshape(
                    lkFrame.frame.width, lkFrame.frame.height, 3
                ),
                format=LK_TO_AV_FORMAT[lkFrame.frame.type],
            )
            frame = frame.reformat(self.width, self.height, format="yuv420p")
            frame.time_base = fractions.Fraction(1, 90_000)
            frame.pts = int(
                (lkFrame.timestamp_us - self.firstStamp)
                * (MICROSECOND_TO_90KHZ_FACTOR)
            )
            self.frameCount += 1
            return frame
        except StopAsyncIteration:
            self.done = True
            return None
        except Exception:
            self.done = True
            traceback.print_exc()
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
            traceback.print_exc()
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
            if self.cached_session_token == "":
                response = await client.post(
                    f"{self.simliHTTPURL}/compose/token",
                    json=self.config,
                    headers={"x-simli-api-key": self.api_key},
                )

                session_token_response = response
                if not session_token_response.is_success:
                    if (
                        session_token_response.status_code >= 400
                        and session_token_response.status_code < 500
                    ):
                        failReason = session_token_response.json()
                        logger.error(failReason["detail"])
                        self.cached_session_token: str = failReason[
                            "session_token"
                        ]
                    raise Exception(SimliExceptions(failReason["detail"]))
                self.cached_session_token: str = (
                    session_token_response.json()
                )["session_token"]

        url_parts = list(
            urlparse.urlparse(f"{self.simliWSURL}/compose/webrtc/livekit")
        )
        query = dict(urlparse.parse_qsl(url_parts[4]))
        query.update(
            {
                "session_token": self.cached_session_token,
            }
        )
        url_parts[4] = urlencode(query)

        wsConnection = websockets.asyncio.client.connect(
            urlparse.urlunparse(url_parts)
        )
        self.wsConnection = await wsConnection.__aenter__()

        self.RegisterEvent(SimliEvent.CONNECTION_INFO, self.AwaitAnswer)
        self.RegisterEvent(SimliEvent.VIDEO_META, self.RegisterVideoInfo)
        self.RegisterEvent(SimliEvent.STOP, self.stop_callback)
        self.RegisterEvent(SimliEvent.ERROR, self.stop_callback)
        self.receiverTask = asyncio.create_task(self.handleMessages())
        self.LocalPeer = rtc.Room()
        self.LocalPeer.on("track_subscribed", self.registerTrack)
        await asyncio.wait(
            [
                asyncio.create_task(self.joinInfoReceived.wait()),
                asyncio.create_task(self.run.event.wait()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not self.joinInfoReceived.is_set():
            raise RuntimeError("Unable to connect to Simli")
        await self.LocalPeer.connect(
            self.sessionJoinInfo["livekit_url"],
            self.sessionJoinInfo["livekit_token"],
        )
        if not self.run.run:
            raise RuntimeError("Connection Failed")

    async def AwaitAnswer(self, joinInfoJSON: str) -> None:
        self.sessionJoinInfo = json.loads(joinInfoJSON)
        self.joinInfoReceived.set()

    async def RegisterVideoInfo(self, VideoInfoJSON: str) -> None:
        parsedMessage = json.loads(VideoInfoJSON)
        self.fps = parsedMessage["video_metadata"]["fps"]
        self.width = parsedMessage["video_metadata"]["width"]
        self.height = parsedMessage["video_metadata"]["height"]
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
        while not self.videoInfoReceived.is_set():
            time.sleep(0.001)
        try:
            if not self.run.run:
                raise RuntimeError("Invalid State")
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
        except Exception:
            traceback.print_exc()
