import asyncio
from simli import SimliClient, SimliConfig
from simli.renderers import NDArrayRenderer
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
            maxSessionLength=20,
            maxIdleTime=10,
        ),
    ) as connection:
        connection.registerSilentEventCallback(silentCallback)
        connection.registerSpeakEventCallback(speakCallback)
        await connection.send(audio)
        await connection.sendSilence()
        videoOut, audioOut = await NDArrayRenderer(connection).render()
        print(videoOut.shape, audioOut.shape)
        print("Done")
        await connection.stop()


asyncio.run(main())
