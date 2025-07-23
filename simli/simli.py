import time
import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Optional


from httpx import AsyncClient, Response
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
    """
    Args:
        apiKey (str): Simli API Key
        faceId (str): Simli Face ID. If using Trinity, you need to specify "faceId/emotionId" in the faceId field to use a different emotion than the default
        handleSilence (bool): Simli server keeps sending silent video when the input buffer is fully depleted. Turning this off makes the video freeze when you don't send in anything
        maxSessionLength (int):
            Absolute maximum session duration, avatar will disconnect after this time
            even if it's speaking.
        maxIdleTime (int):
            Maximum duration the avatar is not speaking for before the avatar disconnects.
    """

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
        self.__ended = False

    async def recv(self) -> VideoFrame:
        try:
            frame: VideoFrame = await self.source.recv()
            if frame is None:
                self.source.stop()
                self.stop()
                return None
            return frame
        except Exception:
            self.stop()
            self.source.stop()
            return None


class AudioFrameReceiver(MediaStreamTrack):
    kind = "audio"

    def __init__(self, source: AudioStreamTrack):
        super().__init__()
        self.source = source
        self.__ended = False

    async def recv(self) -> AudioFrame:
        try:
            frame: AudioFrame = await self.source.recv()
            if frame is None:
                self.stop()
                self.source.stop()
                return None
            return frame
        except Exception:
            self.stop()
            self.source.stop()
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
        simliURL: str = "https://api.simli.ai",
    ):
        """
        :param config: SimliConfig object containing the API Key and Face ID and other optional parameters for the Simli API refer to https://docs.simli.com for more information
        :param useTrunServer: Whether to use the TURN server provided by the Simli API, if set to False, the default STUN server will be used, use only if you are having issues with the default STUN server
        :param latencyInterval: Interval between pings to measure the latency between the client and the simli servers in seconds, set to 0 to disable
        :param simliURL: The URL of the Simli API, defaults to api.simli.ai. Don't change it unless you know what you are doing.
        """
        self.config = config
        self.pc: RTCPeerConnection = None
        self.iceConfig: list[RTCIceServer] = None
        self.ready = asyncio.Event()
        self.run = True
        self.receiverTask: asyncio.Task = None
        self.pingTask: asyncio.Task = None
        self.stopping = False
        self.useTrunServer: bool = useTrunServer
        self.latencyInterval = latencyInterval
        self.simliHTTPURL = simliURL
        self.simliWSURL = simliURL.replace("http", "ws")
        self.tryCount = 3
        self.failErorr = None
        self.speak_event: Optional[Awaitable] = None
        self.silent_event: Optional[Awaitable] = None

    async def Initialize(
        self,
    ):
        """
        Start Simli Connection

        :param get_latency: Interval between pings to measure the latency between the client and the simli servers in seconds, set to 0 to disable
        """
        if self.tryCount == 0:
            raise Exception(
                "Failed to connect to the Simli servers. Last known fail reason: "
                + self.failErorr.__repr__()
            )
        try:
            configJson = self.config.__dict__
            async with AsyncClient() as client:
                requests = []
                requests.append(
                    client.post(
                        f"{self.simliHTTPURL}/startAudioToVideoSession", json=configJson
                    )
                )
                if self.useTrunServer:
                    requests.append(
                        client.post(
                            f"{self.simliHTTPURL}/getIceServers",
                            json={"apiKey": self.config.apiKey},
                        )
                    )

                else:
                    self.iceConfig = [
                        RTCIceServer(
                            urls=[
                                "stun:stun.l.google.com:19302",
                            ]
                        )
                    ]

                responses = await asyncio.gather(*requests)
                session_token_response: Response = responses[0]
                session_token_response.raise_for_status()
                self.session_token = session_token_response.json()["session_token"]
                if self.useTrunServer:
                    self.iceJSON: Response = responses[1]
                    self.iceJSON.raise_for_status()
                    self.iceJSON = self.iceJSON.json()
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
                    f"{self.simliWSURL}/StartWebRTCSession"
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
                self.ready.set()
            await self.pc.setRemoteDescription(
                RTCSessionDescription(**json.loads(answer))
            )
            self.receiverTask = asyncio.create_task(self.handleMessages())

            if self.latencyInterval > 0:
                self.pingTask = asyncio.create_task(self.ping(self.latencyInterval))
        except Exception as e:
            self.failErorr = e
            self.tryCount -= 1
            await self.Initialize()

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
            await self.ready.wait()
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

            elif message == "SILENT" and self.silent_event is not None:
                await self.silent_event()

            elif message == "SPEAK" and self.speak_event is not None:
                await self.speak_event()

            elif message != "ACK":
                print(message)

    def registerSpeakEventCallback(self, async_callback: Awaitable):
        """
        Example:
        ```
        async def callback():
            print("SPEAK")
        simliClient.registerSpeakEventCallback(callback)
        ```
        """
        self.speak_event = async_callback

    def registerSilentEventCallback(self, async_callback: Awaitable):
        """
        Example:
        ```
        async def callback():
            print("SILENT")
        simliClient.registerSpeakEventCallback(callback)
        ```
        """
        self.silent_event = async_callback

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
            await self.wsConnection.send(b"DONE")
        except Exception:
            pass
        try:
            while (
                self.audioReceiver.readyState != "ended"
                and await asyncio.wait_for(self.getNextAudioFrame(), timeout=0.03)
                and drain
            ):
                continue

        except Exception:
            pass
        try:
            while (
                self.videoReceiver.readyState != "ended"
                and await asyncio.wait_for(self.getNextVideoFrame(), timeout=0.03)
                and drain
            ):
                continue
        except Exception:
            pass

        try:
            print("Stopping Simli Connection")
            await self.wsConnection.__aexit__(None, None, None)
            self.receiverTask.cancel()
            if self.pingTask:
                self.pingTask.cancel()
            print("Websocket closed")
            if self.pc.connectionState != "closed":
                await self.pc.close()
        except Exception:
            pass

    async def send(self, data: str | bytes):
        """
        Sends Audio data or control messages to the simli servers
        """
        if not self.ready.is_set():
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
        await self.ready.wait()
        first = True
        while True:
            try:
                if first:
                    frame = await self.videoReceiver.recv()
                    first = False
                else:
                    frame = await asyncio.wait_for(self.videoReceiver.recv(), timeout=1)
            except asyncio.TimeoutError:
                print("Video Stream Timed Out")
                return
            except Exception:
                print("Video Stream Ended")
                return
            if frame is None:
                print("Video Stream Ended")
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
                    first = False
                else:
                    frame = await asyncio.wait_for(self.audioReceiver.recv(), timeout=1)
            except asyncio.TimeoutError:
                print("Audio Stream Timed Out")
                return
            except Exception:
                print("Audio Stream Ended")
                return
            if frame is None:
                print("Audio Stream Ended")
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
