from enum import Enum
import fractions
import time
import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Optional


from httpx import AsyncClient, Response
from av import VideoFrame, AudioFrame
from av.audio.resampler import AudioResampler
from livekit import rtc
import numpy as np
import websockets.asyncio.client


class SimliModels(str, Enum):
    fasttalk = "fasttalk"
    artalk = "artalk"


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
    model: SimliModels = SimliModels.fasttalk


class SimliClient:
    """
    SimliConnection is the main class for interacting with the Simli API. It is used to establish a connection with the Simli servers and receive audio and video data from the servers.
    For more information on the Simli API, visit https://docs.simli.com/
    """

    def __init__(
        self,
        config: SimliConfig | None = None,
        session_token: str | None = None,
        latencyInterval: int = 60,
        simliURL: str = "https://api.simli.ai",
        enable_logging: bool = True,
        retry_count: int = 30,
        retry_timeout: float = 15.0,
    ):
        """
        :param config: SimliConfig object containing the API Key and Face ID and other optional parameters for the Simli API refer to https://docs.simli.com for more information
        :param useTrunServer: Whether to use the TURN server provided by the Simli API, if set to False, the default STUN server will be used, use only if you are having issues with the default STUN server
        :param latencyInterval: Interval between pings to measure the latency between the client and the simli servers in seconds, set to 0 to disable
        :param simliURL: The URL of the Simli API, defaults to api.simli.ai. Don't change it unless you know what you are doing.
        """
        if config is None and (session_token is None or session_token == ""):
            raise Exception(
                "Must provide either a session_token or a config, can't provide empty values for both"
            )
        self.enable_logging = enable_logging
        self.config = config
        self.session_token = session_token
        self.pc: rtc.Room = None
        self.ready = asyncio.Event()
        self.run = True
        self.receiverTask: asyncio.Task = None
        self.pingTask: asyncio.Task = None
        self.stopping = False
        self.latencyInterval = latencyInterval
        self.simliHTTPURL = simliURL
        self.simliWSURL = simliURL.replace("http", "ws")
        self.tryCount = retry_count
        self.retryTimeout = retry_timeout
        self.failErorr = None
        self.starting = False
        self.speak_event: Optional[Awaitable] = None
        self.silent_event: Optional[Awaitable] = None
        self.startTime = time.time()
        self.livekit_token = None
        self.livekit_url = None
        self.videoReceiver = None
        self.audioReceiver = None
        self.fps = None
        self.width = None
        self.height = None

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
            if self.starting:
                return
            self.run = True
            self.starting = True
            if self.session_token is None or self.session_token == "":
                configJson = self.config.__dict__
                async with AsyncClient() as client:
                    session_token_response: Response = await client.post(
                        f"{self.simliHTTPURL}/startAudioToVideoSession", json=configJson
                    )
                    if not session_token_response.is_success:
                        print(session_token_response.text)
                    session_token_response.raise_for_status()
                    self.session_token = session_token_response.json()["session_token"]

            self.pc = rtc.Room()
            self.wsConnection: websockets.asyncio.client.ClientConnection = (
                websockets.asyncio.client.connect(
                    f"{self.simliWSURL}/StartWebRTCSessionLivekit"
                )
            )
            self.wsConnection = await self.wsConnection.__aenter__()
            self.receiverTask = asyncio.create_task(self.handleMessages())

            await self.wsConnection.send(self.session_token)
            self.pc.on("track_subscribed", self.registerTrack)
            while self.livekit_url is None:
                await asyncio.sleep(0.001)
            await self.pc.connect(self.livekit_url, self.livekit_token)
            while not self.ready.is_set():
                await asyncio.sleep(0.001)
            self.ready.set()
            await self.sendSilence(1)

            if self.latencyInterval > 0:
                self.pingTask = asyncio.create_task(self.ping(self.latencyInterval))
            self.starting = False
        except Exception as e:
            self.failErorr = e
            if self.enable_logging:
                print(e)
            self.tryCount -= 1
            await self.stop()
            await self.Initialize()

    def registerTrack(
        self,
        track: rtc.RemoteTrack,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ):
        while self.fps is None:
            time.sleep(0.0001)
        if self.enable_logging:
            print("Registering track", track.kind)
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            receiver = AudioFrameReceiver(
                rtc.AudioStream(track, sample_rate=48000, num_channels=2, client=self),
                client=self,
            )
            self.audioReceiver = receiver
        elif track.kind == rtc.TrackKind.KIND_VIDEO:
            receiver = VideoFrameReceiver(
                rtc.VideoStream(track, format=rtc.VideoBufferType.RGB24),
                client=self,
            )
            self.videoReceiver = receiver

    async def handleMessages(self):
        """
        Internal: Handles messages from the websocket connection. Called in the Initialize function
        """
        while self.run:
            message = await self.wsConnection.recv()
            if message == "START":
                self.ready.set()
                await self.sendSilence(1)
            elif message == "STOP":
                self.run = False
                if self.enable_logging:
                    print(
                        "Closing session due to message from server, check logs for more info"
                    )
                await self.stop()
                break

            elif "error" in message:
                if self.enable_logging:
                    print("Error:", message)
                await self.stop()
                break

            elif "pong" in message:
                pingTime = float(message.split(" ")[1])
                if self.enable_logging:
                    print(f"Ping: {time.time() - pingTime}")

            elif message == "SILENT" and self.silent_event is not None:
                await self.silent_event()

            elif message == "SPEAK" and self.speak_event is not None:
                await self.speak_event()

            elif message == "MISSING_SESSION_TOKEN":
                await self.wsConnection.send(self.session_token)

            elif message == "ACK":
                continue
            else:
                try:
                    parsedMessage = json.loads(message)
                    keys = parsedMessage.keys()
                    if "livekit_url" in keys:
                        self.livekit_url = parsedMessage["livekit_url"]
                        self.livekit_token = parsedMessage["livekit_token"]
                    elif "video_metadata" in keys:
                        self.fps = parsedMessage["video_metadata"]["fps"]
                        self.width = parsedMessage["video_metadata"]["width"]
                        self.height = parsedMessage["video_metadata"]["height"]

                    else:
                        if self.enable_logging:
                            print(parsedMessage.keys())
                except Exception:
                    import traceback

                    traceback.print_exc()
                    print("FAILED TO DECODE MESSAGE", message)

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
        self.run = False
        self.livekit_token = None
        self.livekit_url = None
        self.ready.clear()
        try:
            await self.wsConnection.send(b"DONE")
            await self.wsConnection.close()
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
            await self.pc.disconnect()
            if self.enable_logging:
                print("Stopping Simli Connection")
            self.receiverTask.cancel()
            if self.pingTask:
                self.pingTask.cancel()
            if self.enable_logging:
                print("Websocket closed")
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
            if self.enable_logging:
                print(
                    "Websocket closed, stopping, please check the logs for more information"
                )
            await self.stop()

    async def sendImmediate(self, data: bytes):
        if not self.ready.is_set():
            raise Exception("WSDC Not ready, please wait until self.ready is True")

        try:
            await self.wsConnection.send(b"PLAY_IMMEDIATE" + data[:128000])
            size = len(data)
            if size > 128000:
                for i in range(128000, size, 6000):
                    await self.wsConnection.send(data[i : i + 6000])

        except websockets.WebSocketException:
            if self.enable_logging:
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
        while self.run and self.videoReceiver is None:
            await asyncio.sleep(0.001)
        while self.run:
            try:
                frame = await asyncio.wait_for(
                    self.videoReceiver.recv(), self.retryTimeout
                )
                if first:
                    if frame is not None and frame.to_ndarray().sum() != 0:
                        if self.enable_logging:
                            print(
                                "FIRST VIDEO FRAME RECEIVED",
                                time.time() - self.startTime,
                            )
                        first = False
            except asyncio.TimeoutError:
                print("video timeout")
                if first and not self.stopping:
                    await self.stop()
                    await self.Initialize()
                    continue
                else:
                    frame = None
            except Exception as e:
                if self.enable_logging:
                    print("Video Stream Ended due to exception", e)
                await self.stop()
                return
            if frame is None and not self.stopping:
                await self.stop()
                await self.Initialize()
                continue
            if frame is None:
                if self.enable_logging:
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
        while self.run and self.audioReceiver is None:
            await asyncio.sleep(0.001)
        while self.run:
            try:
                currentReceiver = self.audioReceiver
                frame = await asyncio.wait_for(
                    self.audioReceiver.recv(), self.retryTimeout
                )
                if frame is not None and first:
                    first = False
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                if self.enable_logging:
                    print("Audio Stream Ended due to exception", e)
                return
            if first:
                while self.audioReceiver is currentReceiver:
                    await asyncio.sleep(0.001)
                continue
            if not self.starting:
                if frame is None:
                    if self.enable_logging:
                        print("Audio Stream Ended")

                    return
            if resampler:
                frames = resampler.resample(frame)
                for resampled_frame in frames:
                    yield resampled_frame
            else:
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


class VideoFrameReceiver:
    kind = "video"

    def __init__(self, source: rtc.VideoStream, client: SimliClient):
        self.source = source
        self.first = True
        self.frameCount = 0
        self.client = client

    async def recv(self) -> VideoFrame:
        try:
            frame = None
            while self.client.run:
                try:
                    lkFrame: rtc.VideoFrameEvent = await asyncio.wait_for(
                        self.source.__anext__(), 0.1
                    )
                except asyncio.TimeoutError:
                    continue

                if self.first:
                    self.first = False
                    self.startStamp = lkFrame.timestamp_us
                frame = VideoFrame.from_numpy_buffer(
                    np.frombuffer(lkFrame.frame.data, dtype=np.uint8).reshape(
                        lkFrame.frame.width, lkFrame.frame.height, 3
                    ),
                    format="rgb24",
                )

                frame.time_base = fractions.Fraction(1, 90000)
                frame.pts = int(90000 * 1 / self.client.fps * self.frameCount)
                self.frameCount += 1
                break
            return frame

        except Exception:
            import traceback

            traceback.print_exc()
            return None


class AudioFrameReceiver:
    kind = "audio"

    def __init__(self, source: rtc.AudioStream, client: SimliClient):
        super().__init__()
        self.source = source
        self.client = client

    async def recv(self) -> AudioFrame:
        try:
            lkFrame: rtc.AudioFrameEvent = await self.source.__anext__()
            frame = AudioFrame.from_ndarray(
                np.frombuffer(
                    lkFrame.frame.to_wav_bytes()[44:],
                    dtype=np.int16,
                ).reshape(1, -1),
                layout="stereo" if lkFrame.frame.num_channels == 2 else "mono",
            )
            frame.sample_rate = lkFrame.frame.sample_rate

            return frame
        except StopAsyncIteration:
            return None
        except Exception:
            import traceback

            traceback.print_exc()
            return None
