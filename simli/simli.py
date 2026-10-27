import asyncio
import importlib.util
import math
import random
import re
import struct
import warnings
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Self

import websockets.asyncio.client
from av import VideoFrame
from av.audio.resampler import AudioResampler

from .critical_exceptions import SimliError
from .events import SimliEvent
from .logger import logger
from .transports import base_transport
from .transports.base_transport import Run


class SimliModels(str, Enum):
    fasttalk = "fasttalk"
    artalk = "artalk"


class TransportMode(Enum):
    P2P = 1
    LIVEKIT = 2


@dataclass
class CameraSetting:
    """
    Camera offsets on top of the avatar's own framing, Trinity-2 faces only. The defaults are
    neutral. A value outside its supported range is still applied, with a warning logged when
    the session token is created, or by SimliClient.setCamera during the session.

    Args:
        pos_x_off (float): Moves the avatar right, in half canvas widths. Supported range -10 to 10.
        pos_y_off (float): Moves the avatar down, in half canvas heights. Supported range -10 to 10.
        pos_z_off (float):
            Metres the camera backs away from the avatar (negative moves in).
            Supported range -5 to 5.
        rot_x_off (float): Degrees the camera tilts up (pitch). Supported range -180 to 180.
        rot_y_off (float): Degrees the camera pans right (yaw). Supported range -180 to 180.
        rot_z_off (float): Degrees the camera rolls clockwise. Supported range -180 to 180.
        zoom (float): Multiplies the avatar's zoom; 1 is no change. Supported range above 0, up to 4.
        orbit_x_off (float):
            Degrees the camera orbits up over the avatar's head (elevation).
            Supported range -80 to 80.
        orbit_y_off (float):
            Degrees the camera orbits round to its right (azimuth). Supported range -180 to 180.
    """

    pos_x_off: float = 0.0
    pos_y_off: float = 0.0
    pos_z_off: float = 0.0
    rot_x_off: float = 0.0
    rot_y_off: float = 0.0
    rot_z_off: float = 0.0
    zoom: float = 1.0
    orbit_x_off: float = 0.0
    orbit_y_off: float = 0.0


# start() waits before retry n (n = 1, 2, ...): RETRY_BASE_DELAY_SECONDS * 2^(n-1), at most
# RETRY_MAX_DELAY_SECONDS, plus up to RETRY_MAX_JITTER of that at random. 1, 2, 4, 8, 16, 16 ... s
RETRY_BASE_DELAY_SECONDS = 1.0
RETRY_MAX_DELAY_SECONDS = 16.0
RETRY_MAX_JITTER = 0.2


def retryDelay(retry: int) -> float:
    """
    Seconds start() waits before its retry-th retry, the first being 1
    """
    # The exponent capped so a huge retry_count can't overflow
    delay = min(RETRY_BASE_DELAY_SECONDS * 2 ** min(retry - 1, 32), RETRY_MAX_DELAY_SECONDS)
    return delay * (1 + random.uniform(0, RETRY_MAX_JITTER))


# The order a CAM_ message carries CameraSetting's fields in: y comes before x
CAMERA_MESSAGE_ORDER = (
    "pos_y_off",
    "pos_x_off",
    "pos_z_off",
    "zoom",
    "rot_x_off",
    "rot_y_off",
    "rot_z_off",
    "orbit_x_off",
    "orbit_y_off",
)
ASSET_ID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# No upload has the nil UUID; sent as BG_/FG_ it goes back to the avatar's own
NO_ASSET_ID = "00000000-0000-0000-0000-000000000000"


def float32(name: str, value: float) -> bytes:
    """
    value as a little-endian float32. Raises ValueError for NaN, infinity or a value too large
    for a float32, which the Simli server would refuse.
    """
    if not math.isfinite(value):
        raise ValueError(f"{name} {value} is not a finite number")
    try:
        return struct.pack("<f", value)
    except OverflowError:
        raise ValueError(f"{name} {value} is too large for a float32") from None


def cameraMessage(camera: "CameraSetting | None", lerp_seconds: float | None = None) -> bytes:
    """
    The CAM_ message for camera: CAM_ and all of its fields as little-endian float32s in
    CAMERA_MESSAGE_ORDER, then LERP and lerp_seconds when it isn't None. None is the avatar's
    own framing.
    """
    if camera is None:
        camera = CameraSetting()
    message = b"CAM_" + b"".join(
        float32(f"camera {name}", getattr(camera, name)) for name in CAMERA_MESSAGE_ORDER
    )
    if lerp_seconds is not None:
        message += b"LERP" + float32("lerp_seconds", lerp_seconds)
    return message


def assetId(asset_id: str | None) -> str:
    """
    asset_id as a BG_/FG_ message takes it: the nil UUID for None. Raises ValueError for
    anything but a 36 character id.
    """
    if asset_id is None:
        return NO_ASSET_ID
    if not isinstance(asset_id, str) or not ASSET_ID_PATTERN.fullmatch(asset_id):
        raise ValueError(
            f"Invalid asset id {asset_id!r}, expected the 36 character id POST /assets returned"
        )
    return asset_id


@dataclass
class SimliConfig:
    """
    Args:
        faceId (str): Simli Face ID.
        handleSilence (bool): Simli server keeps sending silent video when the input buffer is fully depleted. Turning this off makes the video freeze when you don't send in anything
        maxSessionLength (int):
            Absolute maximum session duration, avatar will disconnect after this time
            even if it's speaking.
        maxIdleTime (int):
            Maximum duration the avatar is not speaking for before the avatar disconnects.
        model (SimliModels): Deprecated, ignored by the server. Every session runs fasttalk.

    The fields below are left out of the request when None, so the server's default applies.

    Args:
        sessionAggregator (str | None)
        startFrame (int | None): Defaults to 0.
        audioInputFormat (str | None): Defaults to "pcm16".
        streaming_width (int | None):
            Width of the streamed video in pixels, Trinity-2 faces only. At least 512, and with
            streaming_height one of 1:1, 16:9 or 9:16. Defaults to 768.
        streaming_height (int | None):
            Height of the streamed video in pixels. See streaming_width. Defaults to 768.
        avatar_position_x (float | None):
            Where the avatar sits across the canvas, 0 (left) to 1 (right). Trinity-2 only.
            Defaults to 0.5.
        avatar_position_y (float | None):
            Where the avatar sits down the canvas, 0 (top) to 1 (bottom). Trinity-2 only.
            Defaults to 1.
        camera (CameraSetting | None):
            The camera the session starts with. Trinity-2 only. None keeps the avatar's framing.
            Move it live with SimliClient.setCamera.
        background_id (str | None):
            One of your uploaded backgrounds (POST /assets/backgrounds) to show behind the
            avatar, by id. Trinity-2 only. None keeps the avatar's own. An id that isn't
            yours is dropped, with a warning in the token response. Change it live with
            SimliClient.setBackground.
        foreground_id (str | None):
            One of your uploaded foregrounds (POST /assets/foregrounds) to lay over the
            avatar, by id. Trinity-2 only. None keeps the avatar's own. Change it live with
            SimliClient.setForeground.
    """

    faceId: str
    handleSilence: bool = True
    maxSessionLength: int = 600
    maxIdleTime: int = 30
    model: SimliModels = SimliModels.fasttalk
    sessionAggregator: str | None = None
    startFrame: int | None = None
    audioInputFormat: str | None = None
    streaming_width: int | None = None
    streaming_height: int | None = None
    avatar_position_x: float | None = None
    avatar_position_y: float | None = None
    camera: CameraSetting | None = None
    background_id: str | None = None
    foreground_id: str | None = None

    def toRequest(self) -> dict:
        """
        The /compose/token request body, without the fields left as None
        """
        return {key: value for key, value in asdict(self).items() if value is not None}


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
        retry_timeout: float | None = None,
        enableSFU: bool = True,
        transport_mode: TransportMode = TransportMode.P2P,
    ):
        """
        :param api_key: Simli API Key
        :param config: SimliConfig object containing the Face ID and other optional parameters for the Simli API refer to https://docs.simli.com for more information
        :param simliURL: The URL of the Simli API, defaults to api.simli.ai. Don't change it unless you know what you are doing.
        :param retry_count: How many times start() tries to connect. Only network failures (SimliConnectionError) and 5xx responses are retried, with exponential backoff: 1, 2, 4, 8, 16, 16 ... seconds before retries 1, 2, 3 ..., each plus up to 20% at random. Anything else, e.g. an error the server reports (an invalid API key or face ID) or a message the SDK doesn't know, raises straight away.
        :param retry_timeout: Deprecated, ignored.
        """
        if retry_timeout is not None:
            warnings.warn(
                "SimliClient's retry_timeout is deprecated and ignored",
                DeprecationWarning,
                stacklevel=2,
            )
        self.config: SimliConfig = config
        self.api_key = api_key
        self.ready: asyncio.Event = asyncio.Event()
        self.run = Run()
        self.receiverTask: asyncio.Task | None = None
        self.pingTask: asyncio.Task | None = None
        self.stopping = False
        self.simliHTTPURL = simliURL
        self.simliWSURL = simliURL.replace("http", "ws")
        self.tryCount = retry_count
        self.retryTimeout = retry_timeout
        self.failError = None
        self.starting = False
        self.speak_event: Callable[[], Awaitable[None]] | None = None
        self.silent_event: Callable[[], Awaitable[None]] | None = None
        self.enableSFU = enableSFU
        self.cached_session_token: str = ""
        self.transport_mode = transport_mode
        self.Connection = None

    async def start(
        self,
    ) -> Self:
        """
        Start Simli Connection

        Only failures known to be transient are retried, up to retry_count tries in all: a
        SimliConnectionError and a 5xx SimliAPIError. Anything else raises straight away. Retry n
        waits retryDelay(n) first: 1, 2, 4, 8, 16, 16 ... seconds, each plus up to 20% at random.

        Raises:
            SimliAPIError: An HTTP request to the Simli API failed, e.g. an invalid API key, an
                unknown face ID or no credits left. Carries the status code and the server's message.
            SimliSessionError: The Simli server refused to start the session, with its message,
                or sent a message the SDK doesn't know (code UNKNOWN_ERROR, also logged as
                "Unknown error from Simli: <message>").
            SimliConnectionError: The connection to Simli failed on every try.
        """
        if self.starting:
            return self
        self.starting = True
        self.stopping = False
        attempts = max(self.tryCount, 1)
        try:
            for attempt in range(1, attempts + 1):
                self.run = Run()
                try:
                    await self.connect()
                    break
                except BaseException as e:
                    # Cancelled included, so nothing is left connected
                    self.failError = e
                    if self.Connection:
                        await self.Connection.stop()
                    retryable = isinstance(e, SimliError) and e.retryable and not self.stopping
                    if not retryable or attempt == attempts:
                        raise
                    logger.warning(
                        f"Failed to connect to Simli (attempt {attempt} of {attempts}): {e}, retrying"
                    )
                    delay = retryDelay(attempt)
                    logger.info(
                        f"Retrying the connection to Simli in {delay:.2f}s "
                        f"(attempt {attempt + 1} of {attempts})"
                    )
                    await asyncio.sleep(delay)
                    if self.stopping:
                        # stop() was called while waiting
                        raise
                    if importlib.util.find_spec("livekit.rtc") is not None:
                        self.transport_mode = TransportMode.LIVEKIT
        finally:
            self.starting = False
        await self.clearBuffer()
        return self

    async def connect(self) -> None:
        match self.transport_mode:
            case TransportMode.P2P:
                from .transports import p2p_transport

                self.Connection = p2p_transport.P2PTransport(
                    self.run,
                    self.api_key,
                    self.config.toRequest(),
                    self.simliWSURL,
                    self.simliHTTPURL,
                    self.enableSFU,
                )
            case TransportMode.LIVEKIT:
                from .transports import livekit_transport

                self.Connection = livekit_transport.LivekitTransport(
                    self.run,
                    self.api_key,
                    self.config.toRequest(),
                    self.simliWSURL,
                    self.simliHTTPURL,
                )
            case _:
                raise ValueError("INVALID TRANSPORT MODE")
        await self.Connection.Connect()

    @property
    def stopReason(self) -> str | None:
        """
        Why the session ended, as the Simli server put it (e.g. "IDLE", "MAX"), or the error that
        ended it. None while it's running or when the client stopped it.
        """
        return self.Connection.stopReason if self.Connection else None

    @property
    def error(self) -> SimliError | None:
        """
        The error that ended the session, None while it's running or if it ended normally
        """
        return self.Connection.error if self.Connection else None

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
        except websockets.WebSocketException as e:
            await self.stop()
            raise await self.Connection.closedError("Sending to Simli") from e

    async def sendImmediate(self, data: bytes):
        if not self.Connection:
            raise RuntimeError("Invalid State")
        try:
            await self.Connection.wsConnection.send(b"PLAY_IMMEDIATE" + data[:128000])
            size = len(data)
            if size > 128000:
                for i in range(128000, size, 6000):
                    await self.Connection.wsConnection.send(data[i : i + 6000])

        except websockets.WebSocketException as e:
            await self.stop()
            raise await self.Connection.closedError("Sending to Simli") from e

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

    async def setCamera(
        self, camera: CameraSetting | None, *, lerp_seconds: float | None = None
    ) -> None:
        """
        Moves the camera, Trinity-2 faces only. camera replaces the whole camera, the one the
        session started with included: its values are offsets from the avatar's own framing,
        not from the previous call. None goes back to the avatar's own framing.

        Values outside their supported range are applied as sent, and the server's warning is
        logged on the simli_client logger, as is a refusal (e.g. a face that isn't Trinity-2).
        Neither ends the session.

        Example:
        ```
        await simliClient.setCamera(CameraSetting(zoom=1.5, orbit_y_off=20), lerp_seconds=2)
        await simliClient.setCamera(None)  # back to the avatar's own framing, at once
        ```

        Args:
            camera (CameraSetting | None): The camera to move to, None for the avatar's own.
            lerp_seconds (float | None):
                Glide there over this many seconds, from wherever the camera is now (part-way
                through an earlier glide, if one is running). None, 0 or less cuts.

        Raises:
            ValueError: A camera value or lerp_seconds is NaN or infinite. Nothing is sent.
            SimliError: The session has ended, as send() raises.
        """
        await self.send(cameraMessage(camera, lerp_seconds))

    async def setBackground(self, background_id: str | None) -> None:
        """
        Puts one of your uploaded backgrounds behind the avatar, Trinity-2 faces only, by the id
        POST /assets/backgrounds returned. None goes back to the avatar's own. It applies a
        moment later, once loaded, and the last one sent wins.

        A refusal (not one of your uploads, couldn't load it, not Trinity-2) is logged as a
        warning on the simli_client logger and the avatar keeps what it had. It doesn't end the
        session.

        Raises:
            ValueError: background_id isn't a 36 character id. Nothing is sent.
            SimliError: The session has ended, as send() raises.
        """
        await self.send(f"BG_{assetId(background_id)}")

    async def setForeground(self, foreground_id: str | None) -> None:
        """
        Lays one of your uploaded foregrounds over the avatar through its transparency,
        Trinity-2 faces only, by the id POST /assets/foregrounds returned. None takes it away.
        Otherwise as setBackground.

        Raises:
            ValueError: foreground_id isn't a 36 character id. Nothing is sent.
            SimliError: The session has ended, as send() raises.
        """
        await self.send(f"FG_{assetId(foreground_id)}")

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
