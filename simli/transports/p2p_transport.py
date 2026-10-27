import asyncio
import json
import urllib.parse as urlparse
from collections.abc import AsyncIterator
from typing import AsyncGenerator
from urllib.parse import urlencode

from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.rtcrtpreceiver import RemoteStreamTrack
from av import AudioFrame, VideoFrame
from httpx import AsyncClient
from websockets import WebSocketException

from ..critical_exceptions import SimliError
from ..events import SimliEvent
from ..logger import logger
from .base_transport import (
    BaseTransport,
    Run,
    apiRequest,
    connectWebsocket,
    createSessionToken,
)


class VideoFrameReceiver:
    kind = "video"
    source: RemoteStreamTrack
    run: Run

    def __init__(self, source: RemoteStreamTrack, run: Run):
        self.source = source
        self.__ended = False
        self.run = run

    async def recv(self) -> VideoFrame | None:
        try:
            frameTask = asyncio.create_task(self.source.recv())
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
            frame = await frameTask
            if isinstance(frame, VideoFrame):
                return frame
            else:
                return None
        except Exception:
            self.stop()
            return None

    def stop(self):
        self.source.stop()
        self.__ended = True


class AudioFrameReceiver:
    kind = "audio"
    source: RemoteStreamTrack
    run: Run

    def __init__(self, source: RemoteStreamTrack, run: Run):
        super().__init__()
        self.source = source
        self.__ended = False
        self.run = run

    async def recv(self) -> AudioFrame | None:
        try:
            frameTask = asyncio.create_task(self.source.recv())
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
            frame = await frameTask
            if isinstance(frame, AudioFrame):
                return frame
            else:
                return None
        except Exception:
            self.stop()
            return None

    def stop(self):
        self.__ended = True
        self.source.stop()


class P2PTransport(BaseTransport):
    audioReceiver: AudioFrameReceiver
    videoReceiver: VideoFrameReceiver
    receiverTask: asyncio.Task
    answer: RTCSessionDescription
    answerReceived: asyncio.Event

    def __init__(
        self,
        run: Run,
        api_key: str,
        config: dict,
        simli_ws_url: str,
        simli_http_url: str,
        enable_sfu: bool,
    ):
        super().__init__(run)
        self.simliWSURL = simli_ws_url
        self.simliHTTPURL = simli_http_url
        self.enableSFU = enable_sfu
        self.config = config
        self.api_key = api_key
        self.iceConfig: list[RTCIceServer] = []
        self.answerReceived = asyncio.Event()
        self.cached_session_token = ""

    async def Connect(self) -> None:
        async with AsyncClient() as client:
            # The session token's error is the one to report when both fail
            tokenResult, iceResult = await asyncio.gather(
                createSessionToken(client, self.simliHTTPURL, self.api_key, self.config),
                apiRequest(
                    client,
                    "GET",
                    f"{self.simliHTTPURL}/compose/ice",
                    "Getting the ICE servers",
                    headers={"x-simli-api-key": self.api_key},
                ),
                return_exceptions=True,
            )
            if isinstance(tokenResult, BaseException):
                raise tokenResult
            if isinstance(iceResult, BaseException):
                raise iceResult
            self.cached_session_token = tokenResult
            for server in iceResult.json():
                self.iceConfig.append(RTCIceServer(**server))

        self.LocalPeer = RTCPeerConnection(RTCConfiguration(iceServers=self.iceConfig))
        self.LocalPeer.addTransceiver("audio", direction="recvonly")
        self.LocalPeer.addTransceiver("video", direction="recvonly")
        self.LocalPeer.on("track", self.RegisterTrack)
        await self.LocalPeer.setLocalDescription(await self.LocalPeer.createOffer())
        while self.LocalPeer.iceGatheringState != "complete":
            await asyncio.sleep(0.001)

        jsonOffer = self.LocalPeer.localDescription.__dict__
        url_parts = list(urlparse.urlparse(f"{self.simliWSURL}/compose/webrtc/p2p"))
        query = dict(urlparse.parse_qsl(url_parts[4]))
        query.update(
            {
                "enableSFU": str(self.enableSFU),
                "session_token": self.cached_session_token,
            }
        )
        url_parts[4] = urlencode(query)

        self.wsConnection = await connectWebsocket(urlparse.urlunparse(url_parts))
        self.RegisterEvent(SimliEvent.CONNECTION_INFO, self.AwaitAnswer)
        self.RegisterEvent(SimliEvent.STOP, self.stop_callback)
        self.RegisterEvent(SimliEvent.ERROR, self.stop_callback)

        self.receiverTask = asyncio.create_task(self.handleMessages())
        try:
            await self.wsConnection.send(json.dumps(jsonOffer))
        except WebSocketException as e:
            raise await self.closedError("Sending the WebRTC offer to Simli") from e
        await self.waitFor(self.answerReceived, "Getting the WebRTC answer from Simli")
        try:
            await self.LocalPeer.setRemoteDescription(self.answer)
        except Exception as e:
            # The answer is wrong, not the network: no retry fixes it
            raise SimliError(f"Setting up WebRTC with Simli failed: {e!r}") from e
        await self.waitFor(self.started, "Starting the Simli session")
        if not self.run.run:
            raise self.endedError("Starting the Simli session")

    async def AwaitAnswer(self, answer: str):
        try:
            self.answer = RTCSessionDescription(**json.loads(answer))
        except (ValueError, TypeError):
            await self.unknownMessage(answer)
            return
        self.answerReceived.set()

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
            await self.LocalPeer.close()
        except Exception:
            pass
        try:
            await self.wsConnection.send(b"DONE")
            await self.wsConnection.close()
        except Exception:
            pass

        logger.info("Stopping Simli Connection")

    def RegisterTrack(self, track: RemoteStreamTrack):
        logger.debug(f"Registering track {track.kind}")
        if track.kind == "audio":
            receiver = AudioFrameReceiver(track, self.run)
            self.audioReceiver = receiver
        elif track.kind == "video":
            receiver = VideoFrameReceiver(track, self.run)
            self.videoReceiver = receiver
