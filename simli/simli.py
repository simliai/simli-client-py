import asyncio
import traceback
from dataclasses import dataclass
from enum import Enum
from typing import AsyncGenerator, Awaitable, Callable, Optional, Self

import websockets.asyncio.client
from av import VideoFrame
from av.audio.resampler import AudioResampler

from .critical_exceptions import SimliExceptions
from .events import SimliEvent
from .logger import logger
from .transports import base_transport, livekit_transport, p2p_transport
from .transports.base_transport import Run


class SimliModels(str, Enum):
    fasttalk = "fasttalk"
    artalk = "artalk"


class TransportMode(Enum):
    P2P = 1
    LIVEKIT = 2


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

    faceId: str
    handleSilence: bool = True
    maxSessionLength: int = 600
    maxIdleTime: int = 30
    model: SimliModels = SimliModels.fasttalk


class SimliClient:
    """
    SimliConnection is the main class for interacting with the Simli API. It is used to establish a connection with the Simli servers and receive audio and video data from the servers.
    For more information on the Simli API, visit https://docs.simli.com/
    """

    Connection: base_transport.BaseTransport | None

    def __init__(
        self,
        api_key: str,
        config: SimliConfig,
        simliURL: str = "https://api.simli.ai",
        retry_count: int = 3,
        retry_timeout: float = 15.0,
        enableSFU: bool = True,
        transport_mode: TransportMode = TransportMode.P2P,
    ):
        """
        :param config: SimliConfig object containing the API Key and Face ID and other optional parameters for the Simli API refer to https://docs.simli.com for more information
        :param latencyInterval: Interval between pings to measure the latency between the client and the simli servers in seconds, set to 0 to disable
        :param simliURL: The URL of the Simli API, defaults to api.simli.ai. Don't change it unless you know what you are doing.
        """
        self.config: SimliConfig = config
        self.api_key = api_key
        self.ready: asyncio.Event = asyncio.Event()
        self.run = Run()
        self.receiverTask: asyncio.Task = None
        self.pingTask: asyncio.Task = None
        self.stopping = False
        self.simliHTTPURL = simliURL
        self.simliWSURL = simliURL.replace("http", "ws")
        self.tryCount = retry_count
        self.retryTimeout = retry_timeout
        self.failError = None
        self.starting = False
        self.speak_event: Optional[Callable[[], Awaitable[None]]] = None
        self.silent_event: Optional[Callable[[], Awaitable[None]]] = None
        self.enableSFU = enableSFU
        self.cached_session_token: str = ""
        self.transport_mode = transport_mode
        self.Connection = None

    async def start(
        self,
    ) -> Self:
        """
        Start Simli Connection

        :param get_latency: Interval between pings to measure the latency between the client and the simli servers in seconds, set to 0 to disable
        """
        if self.tryCount == 0:
            raise Exception(
                "Failed to connect to the Simli servers. Last known fail reason: "
                + str(self.failError)
            )
        try:
            if self.starting:
                return self
            self.starting = True

            match self.transport_mode:
                case TransportMode.P2P:
                    self.Connection = p2p_transport.P2PTransport(
                        self.run,
                        self.api_key,
                        self.config.__dict__,
                        self.simliWSURL,
                        self.simliHTTPURL,
                        self.enableSFU,
                    )
                    await self.Connection.Connect()
                case TransportMode.LIVEKIT:
                    self.Connection = livekit_transport.LivekitTransport(
                        self.run,
                        self.api_key,
                        self.config.__dict__,
                        self.simliWSURL,
                        self.simliHTTPURL,
                    )
                    await self.Connection.Connect()
                case _:
                    raise ValueError("INVALID TRANSPORT MODE")

        except Exception as e:
            if len(e.args) > 0 and SimliExceptions(e.args[0]) != SimliExceptions.UNKNOWN_ERROR:
                self.tryCount = 0

            self.tryCount -= 1
            await self.stop()
            if self.tryCount <= 0:
                raise Exception("Failed To Initialize SimliClient")
            logger.info("Retrying Connection")
            self.transport_mode = TransportMode.LIVEKIT
            traceback.print_exc()
            return await self.start()
        self.starting = False
        return self

    def registerEventCallback(
        self,
        event: SimliEvent,
        async_callback: Callable[[], Awaitable[None]],
    ):
        """
        Example:
        ```
        async def callback():
            print("SPEAK")
        simliClient.registerSpeakEventCallback(callback)
        ```
        """

        async def callback_wrapper(_):
            await async_callback()

        if not self.Connection:
            raise RuntimeError("Invalid State")
        self.Connection.RegisterEvent(event, callback_wrapper)

    async def stop(self):
        """
        Gracefully terminates the connection
        """
        self.starting = False
        if self.stopping:
            return
        self.stopping = True
        if self.Connection:
            await self.Connection.stop()

    async def send(self, data: str | bytes):
        """
        Sends Audio data or control messages to the simli servers
        """
        if not self.Connection:
            raise RuntimeError("Invalid State")
        try:
            for i in range(0, len(data), 6000):
                await self.Connection.wsConnection.send(data[i : i + 6000])
        except websockets.WebSocketException:
            logger.error("Websocket closed, stopping, please check the logs for more information")
            await self.stop()

    async def sendImmediate(self, data: bytes):
        if not self.Connection:
            raise RuntimeError("Invalid State")
        try:
            await self.Connection.wsConnection.send(b"PLAY_IMMEDIATE" + data[:128000])
            size = len(data)
            if size > 128000:
                for i in range(128000, size, 6000):
                    await self.Connection.wsConnection.send(data[i : i + 6000])

        except websockets.WebSocketException:
            logger.error("Websocket closed, stopping, please check the logs for more information")
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

    async def getVideoStreamIterator(
        self, targetFormat: str = "rgb24"
    ) -> AsyncGenerator[VideoFrame, None]:
        """
        Returns the video output as an async iterator with the specified format (default: rgb24)

        Refer to https://pyav.org for more information on the available formats
        """
        if self.Connection is None:
            raise RuntimeError("Invalid State")
        async for frame in self.Connection.VideoFrameIterator():
            if targetFormat != "yuva420p":
                frame = frame.reformat(format=targetFormat)
            yield frame

    async def getAudioStreamIterator(self, targetSampleRate: int = 48000):
        """
        Returns the audio output as an async iterator
        """
        if self.Connection is None:
            raise RuntimeError("Invalid State")
        resampler = None
        if targetSampleRate != 48000:  # default WebRTC sample rate
            resampler = AudioResampler(format="s16", layout="stereo", rate=targetSampleRate)

        async for frame in self.Connection.AudioFrameIterator():
            if resampler is not None:
                for resampled_frame in resampler.resample(frame):
                    yield resampled_frame
            else:
                yield frame

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(self, exc_type, exc, tb):
        await self.stop()
