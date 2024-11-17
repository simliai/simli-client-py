import time
import asyncio
import json
from dataclasses import dataclass

import requests
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceServer,
    RTCConfiguration,
)
from aiortc.mediastreams import MediaStreamTrack, VideoStreamTrack, AudioStreamTrack
from av import VideoFrame, AudioFrame
from av.audio.resampler import AudioResampler
import websockets.asyncio.client


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

    async def recv(self) -> VideoFrame:
        try:
            frame: VideoFrame = await self.source.recv()
            return frame
        except Exception as e:
            print(e)
            return None


class AudioFrameReceiver(MediaStreamTrack):
    kind = "audio"

    def __init__(self, source: AudioStreamTrack):
        super().__init__()
        self.source = source

    async def recv(self) -> AudioFrame:
        try:
            frame: AudioFrame = await self.source.recv()
            return frame
        except Exception as e:
            print(e)
            return None


class SimliClient:
    """
    SimliConnection is the main class for interacting with the Simli API. It is used to establish a connection with the Simli servers and receive audio and video data from the servers.
    For more information on the Simli API, visit https://docs.simli.com/
    """

    def __init__(
        self,
        config: SimliConfig,
        useTrunServer: bool = False,
        latencyInterval: int = 60,
    ):
        """
        :param config: SimliConfig object containing the API Key and Face ID and other optional parameters for the Simli API refer to https://docs.simli.com for more information
        :param useTrunServer: Whether to use the TURN server provided by the Simli API, if set to False, the default STUN server will be used, use only if you are having issues with the default STUN server

        """
        self.config = config
        self.pc: RTCPeerConnection = None
        self.iceConfig: list[RTCIceServer] = None
        self.ready = False
        self.run = True
        self.receiverTask: asyncio.Task = None
        self.pingTask: asyncio.Task = None
        self.stopping = False
        self.useTrunServer: bool = useTrunServer
        self.latencyInterval = latencyInterval

    async def Initialize(
        self,
    ):
        """
        Start Simli Connection

        :param get_latency: Interval between pings to measure the latency between the client and the simli servers in seconds, set to 0 to disable
        """
        configJson = self.config.__dict__

        response = requests.post(
            "https://api.simli.ai/startAudioToVideoSession", json=configJson
        )
        response.raise_for_status()
        self.session_token = response.json()["session_token"]
        if self.useTrunServer:
            self.iceJSON = requests.post(
                "https://api.simli.ai/getIceServers",
                json={"apiKey": self.config.apiKey},
            )
            self.iceJSON.raise_for_status()
            self.iceJSON = self.iceJSON.json()
            self.iceConfig = []
            for server in self.iceJSON:
                self.iceConfig.append(RTCIceServer(**server))
        else:
            self.iceConfig = [
                RTCIceServer(
                    urls=[
                        "stun:stun.l.google.com:19302",
                    ]
                )
            ]
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
            websockets.asyncio.client.connect("wss://api.simli.ai/StartWebRTCSession")
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
        await self.pc.setRemoteDescription(RTCSessionDescription(**json.loads(answer)))
        self.receiverTask = asyncio.create_task(self.handleMessages())

        if self.latencyInterval > 0:
            self.pingTask = asyncio.create_task(self.ping(self.latencyInterval))

    def registerTrack(self, track: MediaStreamTrack):
        print("Registering track", track.kind)
        if track.kind == "audio":
            receiver = AudioFrameReceiver(track)
            self.audioReceiver = receiver
        elif track.kind == "video":
            receiver = VideoFrameReceiver(track)
            self.videoReceiver = receiver

    async def handleMessages(self):
        """
        Internal: Handles messages from the websocket connection. Called in the Initialize function
        """
        while self.run:
            if not self.ready:
                await asyncio.sleep(0.001)
                continue
            message = await self.wsConnection.recv()
            if message == "STOP":
                self.run = False
                print(
                    "Closing session due to hitting the max session length or max idle time"
                )
                await self.stop()
                break

            elif "error" in message:
                print("Error:", message)
                await self.stop()
                break

            elif "pong" in message:
                pingTime = float(message.split(" ")[1])
                print(f"Ping: {time.time() - pingTime}")

            elif message != "ACK":
                print(message)

    async def ping(self, interval: int):
        """
        Internal: Pings the simli servers to measure the latency between the client and the simli servers. Called in the Initialize function
        """
        while self.run:
            pingTime = time.time()
            await self.send(f"ping {pingTime}")
            await asyncio.sleep(interval)

    async def stop(self, drain=False):
        """
        Gracefully terminates the connection
        """
        if self.stopping:
            return
        self.stopping = True
        try:
            await self.wsConnection.send("DONE")
        except Exception:
            pass
        try:
            while (
                await asyncio.wait_for(self.getNextAudioFrame(), timeout=0.03) and drain
            ):
                continue

        except asyncio.TimeoutError:
            pass
        try:
            while (
                await asyncio.wait_for(self.getNextVideoFrame(), timeout=0.03) and drain
            ):
                continue
        except asyncio.TimeoutError:
            pass

        try:
            print("Stopping Simli Connection")
            await self.wsConnection.__aexit__(None, None, None)
            self.receiverTask.cancel()
            if self.pingTask:
                self.pingTask.cancel()
            await self.pc.close()
        except Exception:
            import traceback

            traceback.print_exc()

    async def send(self, data: str | bytes):
        """
        Sends Audio data or control messages to the simli servers
        """
        if not self.ready:
            raise Exception("WSDC Not ready, please wait until self.ready is True")

        try:
            for i in range(0, len(data), 6000):
                await self.wsConnection.send(data[i : i + 6000])
        except websockets.WebSocketException:
            print(
                "Websocket closed, stopping, please check the logs for more information"
            )
            await self.stop()

    async def sendSilence(self, duration: float = 0.1875):
        """
        Sends silence to the simli servers for the specified duration in seconds
        Can be used without args to bootstrap the connection to start receiving silent audio and video frames
        """
        await self.send((0).to_bytes(2, "little") * int(16000 * duration))

    async def clearBuffer(self):
        """
        Clears the buffered audio on the simli servers, useful for interrupting the current audio spoken by the avatar
        """
        await self.send("SKIP")

    async def getVideoStreamIterator(self, targetFormat: str = "rgb24"):
        """
        Returns the video output as an async iterator with the specified format (default: rgb24)

        Refer to https://pyav.org for more information on the available formats
        """
        first = True
        while True:
            try:
                if first:
                    frame = await self.videoReceiver.recv()
                else:
                    frame = await asyncio.wait_for(
                        self.videoReceiver.recv(), timeout=1 / 15
                    )
            except asyncio.TimeoutError:
                return
            if frame is None:
                return
            if targetFormat != "yuva420p":
                frame = frame.reformat(format=targetFormat)
            yield frame

    async def getAudioStreamIterator(self, targetSampleRate: int = 48000):
        """
        Returns the audio output as an async iterator
        """
        resampler = None
        if targetSampleRate != 48000:  # default WebRTC sample rate
            resampler = AudioResampler(
                format="s16", layout="stereo", rate=targetSampleRate
            )
        first = True
        while True:
            try:
                if first:
                    frame = await self.audioReceiver.recv()
                else:
                    frame = await asyncio.wait_for(
                        self.audioReceiver.recv(), timeout=0.04
                    )
            except asyncio.TimeoutError:
                return
            if frame is None:
                return
            if resampler:
                frame = resampler.resample(frame)[0]
            yield frame

    async def getNextVideoFrame(self):
        """
        Returns the next video frame in the specified format (default: rgb24)
        """
        return await self.videoReceiver.recv()

    async def getNextAudioFrame(self):
        """
        Returns the next audio frame
        """
        return await self.audioReceiver.recv()

    async def __aenter__(self):
        await self.Initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.stop()


async def consumeTrack(
    track: MediaStreamTrack,
    connection: SimliClient,
):
    """
    Used for debugging without dumping the output anywhere, just consumes the track and prints the data
    """
    while connection.run:
        print(await track.recv())
