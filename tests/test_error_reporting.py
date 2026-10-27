"""
Offline tests of how SimliClient reports failures. The HTTP API is mocked with
httpx.MockTransport and the signaling websocket is a local server speaking the Simli protocol.

python -m unittest discover -s tests
"""

import asyncio
import json
import unittest
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from unittest import mock

import httpx
from livekit import rtc
from websockets.asyncio.server import ServerConnection, serve
from websockets.frames import CloseCode

from simli import (
    SimliAPIError,
    SimliClient,
    SimliConfig,
    SimliConnectionError,
    SimliError,
    SimliExceptions,
    SimliSessionError,
)
from simli import simli as simliModule
from simli.events import SimliEvent
from simli.simli import TransportMode, retryDelay
from simli.transports import livekit_transport, p2p_transport

FACE_ID = "bf13113a-355a-4d97-8b90-4edccd1da7b2"
DESTINATION = json.dumps({"destination": "worker", "session_id": "session"})
JOIN_INFO = json.dumps({"livekit_url": "wss://livekit.invalid", "livekit_token": "token"})
VIDEO_METADATA = json.dumps({"video_metadata": {"fps": 30, "width": 512, "height": 512}})


async def drain(connection: ServerConnection, received: list) -> None:
    async for message in connection:
        received.append(message)


class FakeSimli:
    """
    The Simli API: the HTTP endpoints answer with token and ice, the websocket runs script
    """

    def __init__(
        self,
        script: Callable[["FakeSimli", ServerConnection], Awaitable[None]] | None = None,
        token=(200, {"session_token": "token", "warnings": []}),
        ice=(200, []),
        rejectWebsocket: tuple[HTTPStatus, str] | None = None,
        instantRetries: bool = True,
    ):
        self.script = script
        self.instantRetries = instantRetries
        self.token = token
        self.ice = ice
        self.rejectWebsocket = rejectWebsocket
        self.paths: list[str] = []
        self.received: list = []
        self.connections = 0
        self.started = asyncio.Event()
        self.endSession = asyncio.Event()

    def handleHTTP(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        response = self.token if request.url.path == "/compose/token" else self.ice
        if isinstance(response, Exception):
            raise response
        status, body = response
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    def processRequest(self, connection: ServerConnection, request):
        if self.rejectWebsocket is not None:
            status, text = self.rejectWebsocket
            return connection.respond(status, text)
        return None

    async def handleWebsocket(self, connection: ServerConnection) -> None:
        self.connections += 1
        assert self.script is not None
        await self.script(self, connection)

    def client(self, transport_mode: TransportMode, retry_count: int = 3) -> SimliClient:
        return SimliClient(
            api_key="key",
            config=SimliConfig(FACE_ID),
            simliURL=self.url,
            retry_count=retry_count,
            transport_mode=transport_mode,
        )

    async def __aenter__(self) -> "FakeSimli":
        self.server = await serve(
            self.handleWebsocket, "127.0.0.1", 0, process_request=self.processRequest
        )
        port = list(self.server.sockets)[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

        def httpClient():
            return httpx.AsyncClient(transport=httpx.MockTransport(self.handleHTTP))

        self.roomConnect = mock.AsyncMock()
        self.patches = [
            mock.patch.object(p2p_transport, "AsyncClient", httpClient),
            mock.patch.object(livekit_transport, "AsyncClient", httpClient),
            mock.patch.object(rtc.Room, "connect", self.roomConnect),
        ]
        if self.instantRetries:
            # The backoff still runs, waiting 0s
            self.patches.append(mock.patch.object(simliModule, "RETRY_BASE_DELAY_SECONDS", 0))
        for patch in self.patches:
            patch.start()
        return self

    async def __aexit__(self, *_):
        for patch in self.patches:
            patch.stop()
        self.ending = ()
        self.endSession.set()
        self.server.close()
        await asyncio.wait_for(self.server.wait_closed(), 5)


def refuseWith(*messages: str):
    """
    A websocket that refuses the session the way the Simli server does
    """

    async def script(fake: FakeSimli, connection: ServerConnection):
        for message in messages:
            await connection.send(message)
        await drain(connection, fake.received)

    return script


async def livekitSession(fake: FakeSimli, connection: ServerConnection):
    """
    A LiveKit session that runs until fake.endSession is set, then sends fake.ending
    """
    for message in (DESTINATION, JOIN_INFO, VIDEO_METADATA, "START"):
        await connection.send(message)
    fake.received.append(await connection.recv())
    fake.started.set()
    await fake.endSession.wait()
    for message in fake.ending:
        await connection.send(message)
    await drain(connection, fake.received)


class TokenErrors(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_face_id_raises_the_server_message_without_retrying(self):
        token = (400, {"detail": "INVALID_FACE_ID", "session_token": "error token"})
        async with FakeSimli(token=token) as fake:
            with self.assertRaises(SimliAPIError) as raised:
                await fake.client(TransportMode.P2P).start()
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "INVALID_FACE_ID")
        self.assertEqual(raised.exception.code, SimliExceptions.INVALID_FACE_ID)
        self.assertIn("HTTP 400: INVALID_FACE_ID", str(raised.exception))
        self.assertEqual(fake.paths.count("/compose/token"), 1)
        self.assertEqual(fake.connections, 0)

    async def test_no_credits_left_keeps_the_billing_message(self):
        detail = "Free credits ran out, upgrade plan on https://app.simli.com"
        token = (402, {"detail": detail, "session_token": "error token"})
        async with FakeSimli(token=token) as fake:
            with self.assertRaises(SimliAPIError) as raised:
                await fake.client(TransportMode.LIVEKIT).start()
        self.assertEqual(raised.exception.status_code, 402)
        self.assertEqual(raised.exception.detail, detail)
        self.assertEqual(raised.exception.code, SimliExceptions.BILLING_ERROR)
        self.assertEqual(fake.paths.count("/compose/token"), 1)

    async def test_invalid_api_key(self):
        token = (401, {"detail": "INVALID_API_KEY", "session_token": "error token"})
        async with FakeSimli(token=token) as fake:
            with self.assertRaises(SimliAPIError) as raised:
                await fake.client(TransportMode.P2P).start()
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.code, SimliExceptions.INVALID_API_KEY)

    async def test_plain_text_error_body(self):
        token = (401, "THESE OPTIONS ARE MEANT TO BE SET ONLY BY SIMLI SERVICES")
        async with FakeSimli(token=token) as fake:
            with self.assertRaises(SimliAPIError) as raised:
                await fake.client(TransportMode.P2P).start()
        self.assertEqual(raised.exception.detail, token[1])

    async def test_server_errors_are_retried_then_raised(self):
        token = (500, {"detail": "Internal Server Error"})
        async with FakeSimli(token=token) as fake:
            with self.assertRaises(SimliAPIError) as raised:
                await fake.client(TransportMode.P2P, retry_count=3).start()
        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(fake.paths.count("/compose/token"), 3)

    async def test_ice_server_error(self):
        async with FakeSimli(ice=(502, "Bad Gateway")) as fake:
            with self.assertRaises(SimliAPIError) as raised:
                await fake.client(TransportMode.P2P, retry_count=1).start()
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("ICE", str(raised.exception))
        self.assertIn("Bad Gateway", str(raised.exception))

    async def test_network_failures_are_retried(self):
        for failure in (
            httpx.ReadTimeout("timed out"),
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
        ):
            with self.subTest(failure=type(failure).__name__):
                async with FakeSimli(token=failure) as fake:
                    with self.assertRaises(SimliConnectionError):
                        await fake.client(TransportMode.P2P, retry_count=2).start()
                self.assertEqual(fake.paths.count("/compose/token"), 2)

    async def test_other_request_failures_are_not_retried(self):
        for failure in (
            httpx.UnsupportedProtocol("Request URL is missing an 'http://' or 'https://'"),
            httpx.TooManyRedirects("Exceeded maximum allowed redirects."),
        ):
            with self.subTest(failure=type(failure).__name__):
                async with FakeSimli(token=failure) as fake:
                    with self.assertRaises(SimliError) as raised:
                        await fake.client(TransportMode.LIVEKIT, retry_count=3).start()
                self.assertNotIsInstance(raised.exception, SimliConnectionError)
                self.assertFalse(raised.exception.retryable)
                self.assertEqual(fake.paths.count("/compose/token"), 1)

    async def test_unreachable_api(self):
        async with FakeSimli(token=httpx.ConnectError("Connection refused")) as fake:
            with self.assertRaises(SimliConnectionError) as raised:
                await fake.client(TransportMode.LIVEKIT, retry_count=2).start()
        self.assertIn("Connection refused", str(raised.exception))
        self.assertEqual(fake.paths.count("/compose/token"), 2)


class RetryBackoff(unittest.IsolatedAsyncioTestCase):
    def test_schedule(self):
        with mock.patch.object(simliModule, "random") as rand:
            rand.uniform.return_value = 0
            self.assertEqual([retryDelay(n) for n in range(1, 8)], [1, 2, 4, 8, 16, 16, 16])
            rand.uniform.assert_called_with(0, 0.2)
            self.assertEqual(retryDelay(10_000), 16)
            # The most jitter there is: +20%
            rand.uniform.return_value = 0.2
            expected = [1.2, 2.4, 4.8, 9.6, 19.2, 19.2]
            for retry, delay in enumerate(expected, start=1):
                self.assertAlmostEqual(retryDelay(retry), delay)

    async def test_start_waits_before_each_retry(self):
        token = (503, {"detail": "Service Unavailable"})
        sleep = mock.AsyncMock()
        async with FakeSimli(token=token, instantRetries=False) as fake:
            with (
                mock.patch.object(simliModule, "random") as rand,
                mock.patch("asyncio.sleep", sleep),
                self.assertLogs("simli_client", "INFO") as logs,
            ):
                rand.uniform.return_value = 0.1
                with self.assertRaises(SimliAPIError):
                    await fake.client(TransportMode.P2P, retry_count=7).start()
        self.assertEqual(fake.paths.count("/compose/token"), 7)
        delays = [call.args[0] for call in sleep.await_args_list]
        expected = [1.1, 2.2, 4.4, 8.8, 17.6, 17.6]
        self.assertEqual(len(delays), len(expected))
        for delay, want in zip(delays, expected):
            self.assertAlmostEqual(delay, want)
        for attempt, want in enumerate(expected, start=2):
            message = f"Retrying the connection to Simli in {want:.2f}s (attempt {attempt} of 7)"
            self.assertIn(f"INFO:simli_client:{message}", logs.output)

    async def test_no_wait_without_a_retry(self):
        token = (401, {"detail": "INVALID_API_KEY"})
        sleep = mock.AsyncMock()
        async with FakeSimli(token=token, instantRetries=False) as fake:
            with mock.patch("asyncio.sleep", sleep), self.assertRaises(SimliAPIError):
                await fake.client(TransportMode.P2P, retry_count=3).start()
        sleep.assert_not_awaited()

    async def test_stop_while_waiting_ends_the_retries(self):
        async with FakeSimli(token=(503, "Service Unavailable"), instantRetries=False) as fake:
            client = fake.client(TransportMode.P2P, retry_count=3)

            async def stopWhileWaiting(delay: float):
                await client.stop()

            sleep = mock.AsyncMock(side_effect=stopWhileWaiting)
            with mock.patch("asyncio.sleep", sleep), self.assertRaises(SimliAPIError):
                await client.start()
        self.assertEqual(sleep.await_count, 1)
        self.assertEqual(fake.paths.count("/compose/token"), 1)

    def test_retry_timeout_is_deprecated(self):
        with self.assertWarns(DeprecationWarning):
            SimliClient("key", SimliConfig(FACE_ID), retry_timeout=15)


class SignalingErrors(unittest.IsolatedAsyncioTestCase):
    async def test_p2p_startup_error_raises_the_server_message(self):
        script = refuseWith(
            DESTINATION,
            "ERROR: SESSION_TOKEN_EXPIRED",
            "ENDFRAME:0",
            "CLOSING REASON: SESSION_TOKEN_EXPIRED",
            "STOP",
        )
        async with FakeSimli(script) as fake:
            client = fake.client(TransportMode.P2P)
            with self.assertRaises(SimliSessionError) as raised:
                await client.start()
        self.assertEqual(str(raised.exception), "SESSION_TOKEN_EXPIRED")
        self.assertEqual(fake.connections, 1)
        self.assertEqual(client.stopReason, "SESSION_TOKEN_EXPIRED")

    async def test_livekit_startup_error_raises_the_server_message(self):
        script = refuseWith(DESTINATION, JOIN_INFO, "ERROR: INVALID_FACE_ID", "ENDFRAME:0")
        async with FakeSimli(script) as fake:
            with self.assertRaises(SimliSessionError) as raised:
                await fake.client(TransportMode.LIVEKIT).start()
        self.assertEqual(raised.exception.code, SimliExceptions.INVALID_FACE_ID)
        self.assertEqual(fake.connections, 1)

    async def test_rate_limit(self):
        message = "RATE LIMIT REACHED! MAKE SURE YOU CLOSE ALL CONNECTIONS BEFORE STARTING NEW ONES"
        script = refuseWith(DESTINATION, JOIN_INFO, message, "ENDFRAME:0")
        async with FakeSimli(script) as fake:
            with self.assertRaises(SimliSessionError) as raised:
                await fake.client(TransportMode.LIVEKIT).start()
        self.assertEqual(str(raised.exception), message)
        self.assertEqual(fake.connections, 1)

    async def test_websocket_handshake_rejected(self):
        async with FakeSimli(rejectWebsocket=(HTTPStatus.FORBIDDEN, "Forbidden")) as fake:
            with self.assertRaises(SimliAPIError) as raised:
                await fake.client(TransportMode.LIVEKIT).start()
        self.assertEqual(raised.exception.status_code, 403)

    async def test_websocket_dropped_before_the_session_starts_is_retried(self):
        async def script(fake: FakeSimli, connection: ServerConnection):
            await connection.close(CloseCode.INTERNAL_ERROR, "worker crashed")

        async with FakeSimli(script) as fake:
            with self.assertRaises(SimliConnectionError) as raised:
                await fake.client(TransportMode.LIVEKIT, retry_count=2).start()
        self.assertIn("worker crashed", str(raised.exception))
        self.assertEqual(fake.connections, 2)

    async def test_unknown_message_while_starting_raises_once_with_its_text(self):
        message = "SOMETHING THE SDK HAS NEVER SEEN"
        for transport_mode in (TransportMode.P2P, TransportMode.LIVEKIT):
            with self.subTest(transport_mode=transport_mode.name):
                async with FakeSimli(refuseWith(DESTINATION, message)) as fake:
                    client = fake.client(transport_mode, retry_count=3)
                    with self.assertLogs("simli_client", "ERROR") as logs:
                        with self.assertRaises(SimliSessionError) as raised:
                            await client.start()
                self.assertEqual(raised.exception.message, message)
                self.assertEqual(raised.exception.code, SimliExceptions.UNKNOWN_ERROR)
                self.assertFalse(raised.exception.retryable)
                self.assertEqual(fake.connections, 1)
                self.assertIn(
                    f"ERROR:simli_client:Unknown error from Simli: {message}", logs.output
                )
                self.assertIs(client.error, raised.exception)

    async def test_unknown_json_while_starting_is_not_retried(self):
        message = json.dumps({"surprise": True})
        async with FakeSimli(refuseWith(DESTINATION, message)) as fake:
            with self.assertLogs("simli_client", "ERROR"):
                with self.assertRaises(SimliSessionError) as raised:
                    await fake.client(TransportMode.LIVEKIT, retry_count=3).start()
        self.assertEqual(raised.exception.message, message)
        self.assertEqual(fake.connections, 1)

    async def test_broken_webrtc_answer_is_not_retried(self):
        answer = json.dumps({"sdp": "not an sdp", "type": "answer"})
        async with FakeSimli(refuseWith(DESTINATION, answer)) as fake:
            with self.assertRaises(SimliError) as raised:
                await fake.client(TransportMode.P2P, retry_count=3).start()
        self.assertNotIsInstance(raised.exception, SimliConnectionError)
        self.assertIn("Setting up WebRTC", str(raised.exception))
        self.assertEqual(fake.connections, 1)

    async def test_livekit_connection_failure(self):
        async with FakeSimli(livekitSession) as fake:
            fake.roomConnect.side_effect = rtc.ConnectError("signal connection failed")
            with self.assertRaises(SimliConnectionError) as raised:
                await fake.client(TransportMode.LIVEKIT, retry_count=2).start()
        self.assertIn("signal connection failed", str(raised.exception))
        self.assertEqual(fake.connections, 2)


class SessionEnd(unittest.IsolatedAsyncioTestCase):
    async def startSession(self, fake: FakeSimli, *ending: str) -> SimliClient:
        fake.ending = ending
        client = await fake.client(TransportMode.LIVEKIT).start()
        await asyncio.wait_for(fake.started.wait(), 5)
        self.assertEqual(fake.received, ["SKIP"])
        return client

    async def endSession(self, fake: FakeSimli, client: SimliClient) -> None:
        fake.endSession.set()
        assert client.Connection is not None
        await asyncio.wait_for(client.Connection.receiverTask, 5)

    async def test_stop_reason(self):
        async with FakeSimli(livekitSession) as fake:
            client = await self.startSession(fake, "ENDFRAME:120", "CLOSING REASON: IDLE", "STOP")
            stops = []

            async def onStop():
                stops.append(True)

            client.registerEventCallback(SimliEvent.STOP, onStop)
            await self.endSession(fake, client)
            await asyncio.sleep(0)
            self.assertEqual(client.stopReason, "IDLE")
            self.assertIsNone(client.error)
            self.assertEqual(stops, [True])
            with self.assertRaises(SimliConnectionError) as raised:
                await client.sendSilence()
            self.assertIn("IDLE", str(raised.exception))
            await client.stop()

    async def test_error_mid_session(self):
        async with FakeSimli(livekitSession) as fake:
            client = await self.startSession(fake, "ERROR: SERVER ERROR IN INITIALIZATION")
            await self.endSession(fake, client)
            self.assertIsInstance(client.error, SimliSessionError)
            self.assertEqual(client.stopReason, "SERVER ERROR IN INITIALIZATION")
            with self.assertRaises(SimliSessionError) as raised:
                await client.sendSilence()
            self.assertIs(raised.exception, client.error)

    async def test_unknown_message_after_start_keeps_the_session_running(self):
        message = "SOMETHING THE SDK HAS NEVER SEEN"

        async def script(fake: FakeSimli, connection: ServerConnection):
            for opening in (DESTINATION, JOIN_INFO, VIDEO_METADATA, "START"):
                await connection.send(opening)
            fake.received.append(await connection.recv())
            await connection.send(message)
            await drain(connection, fake.received)

        async with FakeSimli(script) as fake:
            with self.assertLogs("simli_client", "WARNING") as logs:
                client = await fake.client(TransportMode.LIVEKIT).start()
                while f"WARNING:simli_client:Simli: {message}" not in logs.output:
                    await asyncio.sleep(0.01)
            self.assertIsNone(client.error)
            self.assertIsNone(client.stopReason)
            self.assertTrue(client.run.run)
            await client.clearBuffer()
            while fake.received != ["SKIP", "SKIP"]:
                await asyncio.sleep(0.01)
            await client.stop()

    async def test_websocket_lost_mid_session(self):
        async def script(fake: FakeSimli, connection: ServerConnection):
            for message in (DESTINATION, JOIN_INFO, VIDEO_METADATA, "START"):
                await connection.send(message)
            await connection.recv()
            await fake.endSession.wait()
            # No close frame, as when the network drops
            connection.transport.abort()

        async with FakeSimli(script) as fake:
            client = await fake.client(TransportMode.LIVEKIT).start()
            await self.endSession(fake, client)
            self.assertIsInstance(client.error, SimliConnectionError)
            with self.assertRaises(SimliConnectionError):
                await client.sendSilence()


class EventParsing(unittest.TestCase):
    def test_server_messages(self):
        self.assertEqual(SimliEvent("ERROR: INVALID SESSION TOKEN"), SimliEvent.ERROR)
        self.assertEqual(SimliEvent("RATE LIMIT REACHED!"), SimliEvent.ERROR)
        self.assertEqual(SimliEvent("CLOSING REASON: MAX"), SimliEvent.STOP)
        self.assertEqual(SimliEvent("ENDFRAME:10"), SimliEvent.STOP)
        self.assertEqual(SimliEvent("WARNING: zoom out of range"), SimliEvent.UNKNOWN)

    def test_json_messages_by_their_keys(self):
        # As the server sends them, without spaces: a worker id may contain SDP or LIVEKIT
        def compact(message: dict) -> str:
            return json.dumps(message, separators=(",", ":"))

        destination = {"destination": "U0RQLIVEKIT", "session_id": "session"}
        self.assertEqual(SimliEvent(compact(destination)), SimliEvent.DESTINATION)
        answer = {"sdp": "v=0", "type": "answer"}
        self.assertEqual(SimliEvent(compact(answer)), SimliEvent.CONNECTION_INFO)
        self.assertEqual(SimliEvent(JOIN_INFO), SimliEvent.CONNECTION_INFO)
        self.assertEqual(SimliEvent(VIDEO_METADATA), SimliEvent.VIDEO_META)
        self.assertEqual(SimliEvent(compact({"other": 1})), SimliEvent.UNKNOWN)
        self.assertEqual(SimliEvent("{not json"), SimliEvent.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
