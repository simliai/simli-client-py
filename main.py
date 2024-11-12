import asyncio
from dataclasses import dataclass

from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceServer,
    RTCConfiguration,
)
from aiortc.mediastreams import MediaStreamTrack, VideoStreamTrack, AudioStreamTrack
from av import VideoFrame, AudioFrame
import json
from httpx import AsyncClient
import websockets.asyncio.client
import numpy as np


@dataclass
class SimliConfig:
    apiKey: str
    faceId: str
    syncAudio: bool = True
    handleSilence: bool = True
    maxSessionLength: int = 600
    maxIdleTime: int = 30


class VideoFrameReceiver(MediaStreamTrack):
    kind = "video"

    def __init__(self, source: VideoStreamTrack):
        self.source = source

    async def recv(self):
        # startTime = time.time()
        frame: VideoFrame = await self.source.recv()
        ndFrame: np.ndarray = frame.to_rgb().to_ndarray()
        print(ndFrame.shape, "VIDEO")
        return ndFrame


class AudioFrameReceiver(MediaStreamTrack):
    kind = "audio"

    def __init__(self, source: AudioStreamTrack):
        super().__init__()
        self.source = source

    async def recv(self):
        frame: AudioFrame = await self.source.recv()
        print(frame.samples, "AUDIO")
        return frame.to_ndarray()


class SimliConnection:
    def __init__(self, config: SimliConfig):
        self.config = config
        self.pc: RTCPeerConnection = None
        self.iceConfig: list[RTCIceServer] = None
        self.ready = False
        self.run = True

    async def start(self):
        configJson = self.config.__dict__
        async with AsyncClient() as client:
            response = await client.post(
                "https://api.simli.ai/startAudioToVideoSession", json=configJson
            )
            self.session_token = response.json()["session_token"]

            self.iceJSON = (
                await client.post(
                    "https://api.simli.ai/getIceServers",
                    json={"apiKey": self.config.apiKey},
                )
            ).json()
            self.iceConfig = []
            for server in self.iceJSON:
                self.iceConfig.append(RTCIceServer(**server))
            self.pc = RTCPeerConnection(RTCConfiguration(iceServers=self.iceConfig))
            self.pc.addTransceiver("audio", direction="recvonly")
            self.pc.addTransceiver("video", direction="recvonly")
            self.pc.on("track", self.registerTrack)
            self.dc = self.pc.createDataChannel("datachannel", ordered=True)

            await self.pc.setLocalDescription(await self.pc.createOffer())
            while self.pc.iceGatheringState != "complete":
                await asyncio.sleep(0.001)

            jsonOffer = self.pc.localDescription.__dict__
            self.wsConnection: websockets.asyncio.client.ClientConnection = (
                websockets.asyncio.client.connect(
                    "wss://api.simli.ai/StartWebRTCSession"
                )
            )
            self.wsConnection = await self.wsConnection.__aenter__()
            await self.wsConnection.send(json.dumps(jsonOffer))
            await self.wsConnection.recv()  # ACK
            answer = await self.wsConnection.recv()  # ANSWER
            await self.wsConnection.send(self.session_token)
            await self.wsConnection.recv()  # ACK
            ready = await self.wsConnection.recv()  # START MESSAGE
            if ready == "START":
                self.ready = True
            await self.pc.setRemoteDescription(
                RTCSessionDescription(**json.loads(answer))
            )
            self.receiverTask = asyncio.create_task(self.receiver())
            await self.send((0).to_bytes(1, "little") * 6000)
            while self.run:
                await asyncio.sleep(0.001)

    def registerTrack(self, track: MediaStreamTrack):
        print("Registering track", track.kind)
        if track.kind == "audio":
            self.audioReceiver = AudioFrameReceiver(track)
            # asyncio.create_task(consumeTrack(self.audioReceiver, self))
        elif track.kind == "video":
            self.videoReceiver = VideoFrameReceiver(track)
            # asyncio.create_task(consumeTrack(self.videoReceiver, self))

    async def receiver(self):
        while self.run:
            if not self.ready:
                await asyncio.sleep(0.001)
                continue
            message = await self.wsConnection.recv()
            if message == "STOP":
                self.run = False
                break

            if message != "ACK":
                print(message)

    async def stop(self):
        self.receiverTask.cancel()
        await self.pc.close()
        await self.wsConnection.__aexit__(None, None, None)

    async def send(self, data: str | bytes):
        if not self.ready:
            print("WSDC Not ready")
            return
        await self.wsConnection.send(data)

    async def clearBuffer(self):
        await self.send("SKIP")


async def consumeTrack(
    track: MediaStreamTrack, connection: SimliConnection
):  # Used for debugging without dumping the output anywhere
    while connection.run:
        _ = await track.recv()


connection = SimliConnection(
    SimliConfig(
        "",  # API Key
        "",  # Face ID
    )
)
asyncio.run(connection.start())
