import asyncio
from simli import SimliClient, SimliConfig
from simli.renderers import FileRenderer, NDArrayRenderer
import os
from dotenv import load_dotenv

load_dotenv(".env")

with open("test_audio.raw", "rb") as f:
    audio = f.read()


async def speakCallback():
    print("SPEAK CALLBACK")


async def silentCallback():
    print("SILENT CALLBACK")


async def main():
    async with SimliClient(
        SimliConfig(
            os.getenv("SIMLI_API_KEY", ""),  # API Key
            os.getenv("SIMLI_FACE_ID", ""),  # Face ID
        ),
    ) as connection:
        connection.registerSilentEventCallback(silentCallback)
        connection.registerSpeakEventCallback(speakCallback)
        await connection.send(audio)
        await connection.sendSilence()
        renderTask = asyncio.create_task(NDArrayRenderer(connection).render())
        await asyncio.sleep(10)
        print("Done")
        await connection.stop()
        videoOut, audioOut = await renderTask
        print(videoOut.shape, audioOut.shape)
        await renderTask


asyncio.run(main())
