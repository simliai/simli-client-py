import asyncio
from abc import ABC, abstractmethod
from typing import AsyncIterator, Awaitable, Callable

from av import AudioFrame, VideoFrame
from websockets import ClientConnection, WebSocketException

from ..events import SimliEvent
from ..logger import logger


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


class BaseTransport(ABC):
    wsConnection: ClientConnection
    run: Run

    def __init__(self, run: Run):
        self.messageQueue: asyncio.Queue[SimliEvent] = asyncio.Queue()
        self.callbacks: dict[SimliEvent, set[Callable[[str], Awaitable[None]]]] = {
            event: set() for event in SimliEvent
        }
        self.run = run

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

    async def handleMessages(self) -> None:
        """
        Internal: Handles messages from the websocket connection. Called in the Initialize function
        """

        while self.run.run:
            try:
                message = await self.wsConnection.recv()
            except WebSocketException:
                self.run.run = False
                continue
            if isinstance(message, bytes):
                try:
                    message = message.decode()
                except Exception:
                    logger.exception(f"UNABLE TO PARSE SERVER MESSAGE OF LENGTH {len(message)}")
                    continue

            received_event = SimliEvent(message)
            if received_event != SimliEvent.UNKNOWN:
                if received_event in [SimliEvent.ERROR, SimliEvent.STOP]:
                    logger.error(message)
                    await self.stop()

                for event in self.callbacks[received_event]:
                    asyncio.ensure_future(event(message))

            else:
                logger.exception(f"UNABLE TO PARSE SERVER MESSAGE OF LENGTH {len(message)}")
                continue

    def RegisterEvent(self, event: SimliEvent, callback: Callable[[str], Awaitable[None]]) -> None:
        self.callbacks[event].add(callback)
