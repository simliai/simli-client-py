import asyncio
import json
import urllib.parse as urlparse
from collections.abc import AsyncIterator
from types import CoroutineType
from typing import Any, AsyncGenerator
from urllib.parse import urlencode

import websockets.asyncio.client
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.rtcrtpreceiver import RemoteStreamTrack
from av import AudioFrame, VideoFrame
from httpx import AsyncClient, Response

from ..critical_exceptions import SimliExceptions
from ..events import SimliEvent
from ..logger import logger
from .base_transport import BaseTransport, Run


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
            requests: list[CoroutineType[Any, Any, Response]] = []
            if self.cached_session_token == "":
                requests.append(
                    client.post(
                        f"{self.simliHTTPURL}/compose/token",
                        json=self.config,
                        headers={"x-simli-api-key": self.api_key},
                    )
                )
            if len(self.iceConfig) == 0:
                requests.append(
                    client.get(
                        f"{self.simliHTTPURL}/compose/ice",
                        headers={"x-simli-api-key": self.api_key},
                    )
                )
            if len(requests) > 0:
                responses = await asyncio.gather(*requests)
                session_token_response = responses[0]
                if not session_token_response.is_success:
                    if (
                        session_token_response.status_code >= 400
                        and session_token_response.status_code < 500
                    ):
                        failReason = session_token_response.json()
                        logger.error(failReason["detail"])
                        self.cached_session_token: str = failReason["session_token"]
                    raise Exception(SimliExceptions(failReason["detail"]))
                self.cached_session_token: str = (session_token_response.json())["session_token"]
                self.iceJSON: Response = responses[1]
                if not self.iceJSON.is_success:
                    logger.error("FAILED TO GATHER ICE SERVERS")
                    self.iceJSON.raise_for_status()

                self.iceJSON.raise_for_status()
                self.iceJSON = self.iceJSON.json()
                for server in self.iceJSON:
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

        wsConnection = websockets.asyncio.client.connect(urlparse.urlunparse(url_parts))
        self.wsConnection = await wsConnection.__aenter__()
        self.RegisterEvent(SimliEvent.ANSWER, self.AwaitAnswer)
        self.RegisterEvent(SimliEvent.STOP, self.stop_callback)
        self.RegisterEvent(SimliEvent.ERROR, self.stop_callback)

        self.receiverTask = asyncio.create_task(self.handleMessages())
        await self.wsConnection.send(json.dumps(jsonOffer))
        await asyncio.wait(
            [
                asyncio.create_task(self.answerReceived.wait()),
                asyncio.create_task(self.run.event.wait()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not self.answerReceived.is_set():
            raise RuntimeError("Unable to connect to Simli")
        await self.LocalPeer.setRemoteDescription(self.answer)
        if not self.run.run:
            raise RuntimeError("Connection Failed")

    async def AwaitAnswer(self, answer: str):
        self.answer = RTCSessionDescription(**json.loads(answer))
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
