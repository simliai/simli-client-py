import asyncio
import json
import re
from abc import ABC, abstractmethod
from typing import AsyncIterator, Awaitable, Callable

import websockets.asyncio.client
from av import AudioFrame, VideoFrame
from httpx import (
    AsyncClient,
    HTTPError,
    NetworkError,
    RemoteProtocolError,
    Response,
    TimeoutException,
)
from websockets import (
    ClientConnection,
    ConnectionClosed,
    ConnectionClosedError,
    InvalidMessage,
    InvalidStatus,
    WebSocketException,
)

from ..critical_exceptions import (
    SimliAPIError,
    SimliConnectionError,
    SimliError,
    SimliExceptions,
    SimliSessionError,
)
from ..events import SimliEvent
from ..logger import logger

# The server's replies to the client's control messages (the camera, background and foreground
# notices of its asyncapi, and pong): notices, which never end the session
SERVER_NOTICE = re.compile(
    r"WARNING: |pong|Invalid camera override received$|Selected model does not support |"
    r"Invalid (background|foreground) message: |"
    r"(background|foreground) \S+ is not one of your uploaded |"
    r"Couldn't load (background|foreground) "
)


class Run:
    event: asyncio.Event

    def __init__(self):
        self.event = asyncio.Event()

    @property
    def run(
        self,
    ) -> bool:
        return not self.event.is_set()

    @run.setter
    def run(self, v: bool):
        if not v:
            self.event.set()


def apiError(response: Response, request: str) -> SimliAPIError:
    """
    The error for a failed response from the Simli API, with the server's message
    """
    detail = None
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("detail")
    except ValueError:
        pass
    if detail is None:
        detail = response.text.strip() or response.reason_phrase
    elif not isinstance(detail, str):
        # e.g. the request validation errors of a 422
        detail = json.dumps(detail)
    return SimliAPIError(
        f"{request} failed with HTTP {response.status_code}: {detail}",
        status_code=response.status_code,
        detail=detail,
        code=SimliExceptions.fromServerMessage(detail, response.status_code),
    )


async def apiRequest(
    client: AsyncClient, method: str, url: str, request: str, **kwargs
) -> Response:
    """
    Sends a request to the Simli API. Raises SimliAPIError for an error response,
    SimliConnectionError when the network failed or timed out, and SimliError when the request
    failed some other way.
    """
    try:
        response = await client.request(method, url, **kwargs)
    except (NetworkError, TimeoutException, RemoteProtocolError) as e:
        raise SimliConnectionError(f"{request} failed: {e!r}") from e
    except HTTPError as e:
        # e.g. a URL with no scheme or a broken proxy, which no retry fixes
        raise SimliError(f"{request} failed: {e!r}") from e
    if not response.is_success:
        raise apiError(response, request)
    return response


async def createSessionToken(
    client: AsyncClient, simli_http_url: str, api_key: str, config: dict
) -> str:
    response = await apiRequest(
        client,
        "POST",
        f"{simli_http_url}/compose/token",
        "Creating the Simli session token",
        json=config,
        headers={"x-simli-api-key": api_key},
    )
    tokenResponse = response.json()
    for warning in tokenResponse.get("warnings", []):
        logger.warning(warning)
    return tokenResponse["session_token"]


async def connectWebsocket(url: str) -> ClientConnection:
    try:
        return await websockets.asyncio.client.connect(url)
    except InvalidStatus as e:
        detail = e.response.body.decode(errors="replace").strip() if e.response.body else ""
        detail = detail or e.response.reason_phrase
        raise SimliAPIError(
            f"Opening the Simli websocket failed with HTTP {e.response.status_code}: {detail}",
            status_code=e.response.status_code,
            detail=detail,
        ) from e
    except (OSError, TimeoutError, InvalidMessage, ConnectionClosed) as e:
        # Refused, unreachable, timed out or dropped during the handshake
        raise SimliConnectionError(f"Opening the Simli websocket failed: {e!r}") from e
    except WebSocketException as e:
        # e.g. an invalid URI or a handshake the server answered wrongly, which no retry fixes
        raise SimliError(f"Opening the Simli websocket failed: {e!r}") from e


class BaseTransport(ABC):
    wsConnection: ClientConnection
    receiverTask: asyncio.Task
    run: Run

    def __init__(self, run: Run):
        self.messageQueue: asyncio.Queue[SimliEvent] = asyncio.Queue()
        self.callbacks: dict[SimliEvent, set[Callable[[str], Awaitable[None]]]] = {
            event: set() for event in SimliEvent
        }
        self.run = run
        self.started = asyncio.Event()
        self.stopped = False
        # Why the session ended as the server put it, and the error that ended it if one did
        self.stopReason: str | None = None
        self.error: SimliError | None = None

    @abstractmethod
    async def Connect(self) -> None: ...

    @abstractmethod
    def AudioFrameIterator(self) -> AsyncIterator[AudioFrame]: ...

    @abstractmethod
    def VideoFrameIterator(self) -> AsyncIterator[VideoFrame]: ...

    @abstractmethod
    async def stop(self) -> None: ...

    async def stop_callback(self, _):
        await self.stop()

    def setError(self, error: SimliError) -> None:
        """
        Records the error that ended the session, the first one wins
        """
        if self.error is None:
            self.error = error
            # Before the session starts, start() raises it
            if self.started.is_set():
                logger.error(f"Simli session failed: {error}")
        if self.stopReason is None:
            self.stopReason = error.message

    def endedError(self, what: str) -> SimliError:
        """
        The error for what failing because the session ended
        """
        if self.error is not None:
            return self.error
        if self.stopReason:
            return SimliConnectionError(f"{what} failed, the session ended: {self.stopReason}")
        return SimliConnectionError(f"{what} failed, the connection to Simli is closed")

    async def closedError(self, what: str) -> SimliError:
        """
        endedError, once the messages the server sent before the websocket closed are read
        """
        receiverTask: asyncio.Task | None = getattr(self, "receiverTask", None)
        if receiverTask is not None and receiverTask is not asyncio.current_task():
            await asyncio.wait([receiverTask], timeout=1)
        return self.endedError(what)

    async def unknownMessage(self, message: str) -> None:
        """
        A message from the server the SDK doesn't know. While the session is starting it ends
        the attempt with a SimliSessionError, which start() raises without retrying: no retry
        fixes an error the SDK can't read. Once the session has started it's only logged.
        """
        if self.started.is_set():
            logger.warning(f"Simli: {message}")
            return
        logger.error(f"Unknown error from Simli: {message}")
        self.setError(SimliSessionError(message, SimliExceptions.UNKNOWN_ERROR))
        await self.stop()

    async def waitFor(self, event: asyncio.Event, what: str) -> None:
        """
        Waits for event, raising the error that ended the session if it ends first
        """
        eventTask = asyncio.create_task(event.wait())
        stopTask = asyncio.create_task(self.run.event.wait())
        try:
            await asyncio.wait([eventTask, stopTask], return_when=asyncio.FIRST_COMPLETED)
        finally:
            eventTask.cancel()
            stopTask.cancel()
        if not event.is_set():
            raise self.endedError(what)

    async def handleMessages(self) -> None:
        """
        Internal: Handles messages from the websocket connection. Called in the Initialize function

        Reads until the websocket closes rather than until the media ends, so the reason the
        server gives for ending the session still arrives.
        """

        while True:
            try:
                message = await self.wsConnection.recv()
            except WebSocketException as e:
                if isinstance(e, ConnectionClosedError) and not self.stopped:
                    self.setError(SimliConnectionError(f"Simli websocket closed unexpectedly: {e}"))
                await self.stop()
                return
            if isinstance(message, bytes):
                try:
                    message = message.decode()
                except Exception:
                    logger.exception(f"UNABLE TO PARSE SERVER MESSAGE OF LENGTH {len(message)}")
                    continue
            if len(message.strip()) == 0:
                continue

            received_event = SimliEvent(message)
            upperMessage = message.upper()
            match received_event:
                case SimliEvent.START:
                    self.started.set()
                case SimliEvent.ERROR:
                    serverError = message
                    if upperMessage.startswith("ERROR"):
                        serverError = message[len("ERROR") :].lstrip(": ").strip() or message
                    self.setError(
                        SimliSessionError(
                            serverError, SimliExceptions.fromServerMessage(serverError)
                        )
                    )
                    await self.stop()
                case SimliEvent.STOP:
                    if upperMessage.startswith("ENDFRAME"):
                        # Followed by CLOSING REASON and STOP
                        logger.debug(message)
                        continue
                    if upperMessage.startswith(("CLOSING REASON", "STOP REASON")):
                        reason = message.partition(":")[2].strip()
                        if self.stopReason is None and reason:
                            self.stopReason = reason
                        logger.info(f"Simli session closing, reason: {reason}")
                        if upperMessage.startswith("CLOSING"):
                            # STOP follows
                            continue
                    await self.stop()
                case SimliEvent.UNKNOWN:
                    if upperMessage.startswith("CLIENT NOT CONNECTED TO LIVEKIT ROOM"):
                        self.setError(SimliConnectionError(message))
                        await self.stop()
                        received_event = SimliEvent.ERROR
                    elif SERVER_NOTICE.match(message):
                        # e.g. a camera override out of range or a background that isn't one
                        # of the account's
                        logger.warning(f"Simli: {message}")
                        continue
                    else:
                        await self.unknownMessage(message)
                        if self.started.is_set():
                            continue
                        received_event = SimliEvent.ERROR

            for event in self.callbacks[received_event]:
                asyncio.ensure_future(event(message))

    def RegisterEvent(self, event: SimliEvent, callback: Callable[[str], Awaitable[None]]) -> None:
        self.callbacks[event].add(callback)
