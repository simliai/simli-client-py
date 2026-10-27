"""
Offline tests of the in-session controls: setCamera, setBackground and setForeground, on both
transports. The signaling websocket is the local FakeSimli server of test_error_reporting.

python -m unittest discover -s tests
"""

import asyncio
import json
import math
import struct
import unittest

from aiortc import RTCPeerConnection, RTCSessionDescription
from test_error_reporting import DESTINATION, JOIN_INFO, VIDEO_METADATA, FakeSimli
from websockets.asyncio.server import ServerConnection

from simli import CameraSetting, SimliClient, SimliConfig, SimliConnectionError
from simli.simli import TransportMode

ASSET_ID = "6f1c2a9e-1b2c-4d3e-8f90-0123456789ab"
NIL_UUID = "00000000-0000-0000-0000-000000000000"
NEUTRAL = (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
# CameraSetting(pos_x_off=0.5, pos_y_off=-0.25, pos_z_off=1, zoom=2, rot_x_off=3, ...) as the
# server reads it: little-endian float32s, y before x
CAMERA = CameraSetting(
    pos_x_off=0.5,
    pos_y_off=-0.25,
    pos_z_off=1,
    zoom=2,
    rot_x_off=3,
    rot_y_off=4,
    rot_z_off=5,
    orbit_x_off=6,
    orbit_y_off=7,
)
CAMERA_BYTES = bytes.fromhex(
    "000080be"  # pos_y_off -0.25
    "0000003f"  # pos_x_off 0.5
    "0000803f"  # pos_z_off 1
    "00000040"  # zoom 2
    "00004040"  # rot_x_off 3
    "00008040"  # rot_y_off 4
    "0000a040"  # rot_z_off 5
    "0000c040"  # orbit_x_off 6
    "0000e040"  # orbit_y_off 7
)
ZOOM_WARNING = "WARNING: camera zoom 6.0 is outside its supported range (0.0, 4.0]; applied as is"
NOT_UPLOADED = f"background {ASSET_ID} is not one of your uploaded backgrounds, ignoring"


def reply(message: str | bytes) -> list[str]:
    """
    The notices the Simli server sends back to a message, as text
    """
    if isinstance(message, bytes) and message.startswith(b"CAM_"):
        zoom = struct.unpack("<f", message[16:20])[0]
        return [ZOOM_WARNING] if zoom > 4 else []
    if message == f"BG_{ASSET_ID}":
        return [NOT_UPLOADED]
    return []


async def answerOffer(offer: str) -> tuple[RTCPeerConnection, str]:
    """
    A WebRTC answer to the client's offer, enough for it to set up its peer connection. Never
    set as the local description, so this end doesn't try to connect.
    """
    peer = RTCPeerConnection()
    await peer.setRemoteDescription(RTCSessionDescription(**json.loads(offer)))
    answer = await peer.createAnswer()
    return peer, json.dumps({"sdp": answer.sdp, "type": answer.type})


def controlledSession(transport_mode: TransportMode):
    """
    A session that queues every message the client sends on fake.messages and answers each
    with reply(), until fake.endSession is set, then sends fake.ending
    """

    async def script(fake: FakeSimli, connection: ServerConnection):
        peer = None
        if transport_mode == TransportMode.P2P:
            peer, answer = await answerOffer(await connection.recv())
            opening = (DESTINATION, answer, "START")
        else:
            opening = (DESTINATION, JOIN_INFO, VIDEO_METADATA, "START")
        for message in opening:
            await connection.send(message)

        async def receive():
            async for message in connection:
                await fake.messages.put(message)
                for notice in reply(message):
                    await connection.send(notice)

        receiver = asyncio.create_task(receive())
        ending = asyncio.create_task(fake.endSession.wait())
        await asyncio.wait([receiver, ending], return_when=asyncio.FIRST_COMPLETED)
        ending.cancel()
        if not receiver.done():
            for message in fake.ending:
                await connection.send(message)
            await receiver
        if peer is not None:
            await peer.close()

    return script


class SessionControl(unittest.IsolatedAsyncioTestCase):
    transport_mode = TransportMode.LIVEKIT

    async def asyncSetUp(self):
        self.fake = await FakeSimli(controlledSession(self.transport_mode)).__aenter__()
        self.fake.messages = asyncio.Queue()
        self.fake.ending = ()
        self.client = await self.fake.client(self.transport_mode, retry_count=1).start()
        self.assertEqual(await self.nextMessage(), "SKIP")

    async def asyncTearDown(self):
        await self.client.stop()
        await self.fake.__aexit__()

    async def nextMessage(self) -> str | bytes:
        return await asyncio.wait_for(self.fake.messages.get(), 5)

    async def assertNothingSent(self):
        # The next message the server gets is the one sent after the refused call
        await self.client.clearBuffer()
        self.assertEqual(await self.nextMessage(), "SKIP")

    async def test_camera_cut(self):
        await self.client.setCamera(CAMERA)
        message = await self.nextMessage()
        self.assertIsInstance(message, bytes)
        self.assertEqual(len(message), 40)
        self.assertEqual(message, b"CAM_" + CAMERA_BYTES)

    async def test_camera_glide(self):
        await self.client.setCamera(CAMERA, lerp_seconds=2.5)
        message = await self.nextMessage()
        self.assertEqual(len(message), 48)
        self.assertEqual(message, b"CAM_" + CAMERA_BYTES + b"LERP" + bytes.fromhex("00002040"))

    async def test_camera_partial_setting_sends_all_nine_values(self):
        await self.client.setCamera(CameraSetting(pos_x_off=0.5), lerp_seconds=0)
        message = await self.nextMessage()
        self.assertEqual(len(message), 48)
        self.assertEqual(
            struct.unpack("<4s9f4sf", message),
            (b"CAM_", 0.0, 0.5, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, b"LERP", 0.0),
        )

    async def test_camera_reset(self):
        await self.client.setCamera(None)
        self.assertEqual(await self.nextMessage(), b"CAM_" + struct.pack("<9f", *NEUTRAL))
        await self.client.setCamera(None, lerp_seconds=1)
        self.assertEqual(
            await self.nextMessage(),
            b"CAM_" + struct.pack("<9f", *NEUTRAL) + b"LERP" + struct.pack("<f", 1),
        )

    async def test_camera_refuses_nan_and_infinity(self):
        bad = [
            (CameraSetting(zoom=math.nan), {}, "camera zoom nan"),
            (CameraSetting(pos_x_off=math.inf), {}, "camera pos_x_off inf"),
            (CameraSetting(orbit_y_off=-math.inf), {}, "camera orbit_y_off -inf"),
            (CameraSetting(rot_z_off=1e39), {}, "camera rot_z_off"),
            (None, {"lerp_seconds": math.nan}, "lerp_seconds nan"),
            (CAMERA, {"lerp_seconds": math.inf}, "lerp_seconds inf"),
        ]
        for camera, kwargs, error in bad:
            with self.subTest(error), self.assertRaises(ValueError) as raised:
                await self.client.setCamera(camera, **kwargs)
            self.assertIn(error, str(raised.exception))
        await self.assertNothingSent()

    async def test_camera_out_of_range_is_sent_as_is_and_the_warning_logged(self):
        with self.assertLogs("simli_client", "WARNING") as logs:
            await self.client.setCamera(CameraSetting(zoom=6))
            message = await self.nextMessage()
            await self.waitForLog(logs, ZOOM_WARNING)
        self.assertEqual(struct.unpack("<f", message[16:20]), (6.0,))
        await self.assertSessionRuns()

    async def test_background_and_foreground(self):
        await self.client.setBackground(ASSET_ID.upper())
        self.assertEqual(await self.nextMessage(), f"BG_{ASSET_ID.upper()}")
        await self.client.setForeground(ASSET_ID)
        self.assertEqual(await self.nextMessage(), f"FG_{ASSET_ID}")

    async def test_none_sends_the_nil_uuid(self):
        await self.client.setBackground(None)
        self.assertEqual(await self.nextMessage(), f"BG_{NIL_UUID}")
        await self.client.setForeground(None)
        self.assertEqual(await self.nextMessage(), f"FG_{NIL_UUID}")

    async def test_invalid_asset_ids(self):
        bad = [
            "",
            "background.png",
            ASSET_ID.replace("-", ""),
            f"{{{ASSET_ID}}}",
            f"{ASSET_ID}\n",
            ASSET_ID[:-1] + "g",
            "data:image/png;base64,iVBORw0KGgo=",
        ]
        for method in (self.client.setBackground, self.client.setForeground):
            for assetId in bad:
                with self.subTest(method=method.__name__, assetId=assetId):
                    with self.assertRaises(ValueError) as raised:
                        await method(assetId)
                    self.assertIn("36 character id", str(raised.exception))
        await self.assertNothingSent()

    async def test_asset_notice_is_logged_and_the_session_runs_on(self):
        with self.assertLogs("simli_client", "WARNING") as logs:
            await self.client.setBackground(ASSET_ID)
            await self.nextMessage()
            await self.waitForLog(logs, NOT_UPLOADED)
        await self.assertSessionRuns()

    async def test_controls_raise_once_the_session_has_ended(self):
        self.fake.ending = ("ENDFRAME:120", "CLOSING REASON: IDLE", "STOP")
        self.fake.endSession.set()
        assert self.client.Connection is not None
        await asyncio.wait_for(self.client.Connection.receiverTask, 5)
        controls = [
            self.client.setCamera(CAMERA),
            self.client.setCamera(None, lerp_seconds=1),
            self.client.setBackground(ASSET_ID),
            self.client.setForeground(None),
        ]
        for control in controls:
            with self.assertRaises(SimliConnectionError) as raised:
                await control
            self.assertIn("IDLE", str(raised.exception))

    async def waitForLog(self, logs, text: str) -> None:
        async def logged():
            while not any(text in line for line in logs.output):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(logged(), 5)

    async def assertSessionRuns(self):
        self.assertIsNone(self.client.stopReason)
        self.assertIsNone(self.client.error)
        await self.client.setBackground(None)
        self.assertEqual(await self.nextMessage(), f"BG_{NIL_UUID}")


class P2PSessionControl(SessionControl):
    transport_mode = TransportMode.P2P


class BeforeStart(unittest.IsolatedAsyncioTestCase):
    async def test_controls_need_a_session(self):
        client = SimliClient("key", SimliConfig("face"))
        for control in (
            client.setCamera(None),
            client.setBackground(None),
            client.setForeground(ASSET_ID),
        ):
            with self.assertRaises(RuntimeError):
                await control


if __name__ == "__main__":
    unittest.main()
