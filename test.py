from simli.simli import TransportMode
import asyncio
import logging
import os
import time

from dotenv import load_dotenv

import simli
from simli import SimliClient, SimliConfig
from simli.events import SimliEvent
from simli.renderers.renderers import FileRenderer, NDArrayRenderer

simli.logger.logger.disabled = False
# logging.basicConfig(
#     level=logging.DEBUG,
# )
load_dotenv(".env")

with open("test_audio.raw", "rb") as f:
    audio = f.read()


async def speakCallback():
    print("SPEAK CALLBACK")


async def silentCallback():
    print("SILENT CALLBACK")


async def main():
    s = time.time()
    async with SimliClient(
        api_key=os.getenv("SIMLI_API_KEY", ""),  # API Key
        config=SimliConfig(
            os.getenv("SIMLI_FACE_ID", ""),  # Face ID
            # model="artalk",
        ),
        # transport_mode=TransportMode.LIVEKIT,
    ) as connection:
        print(time.time() - s)
        connection.registerEventCallback(SimliEvent.SPEAK, speakCallback)
        await connection.sendSilence(4)
        await connection.sendImmediate(audio)
        renderTask = asyncio.create_task(FileRenderer(connection, filename="test.mp4").render())
        # renderTask = asyncio.create_task(NDArrayRenderer(connection).render())
        await asyncio.sleep(10)
        print("Done")

    await renderTask


asyncio.run(main())
