import asyncio
from simli import SimliClient, SimliConfig
from simli.renderers import FileRenderer
import os
from dotenv import load_dotenv

load_dotenv(".env")

with open("audio2.raw", "rb") as f:
    audio = f.read()


async def main():
    async with SimliClient(
        SimliConfig(
            os.getenv("SIMLI_API_KEY", ""),  # API Key
            os.getenv("SIMLI_FACE_ID", ""),  # Face ID
            maxSessionLength=20,
            maxIdleTime=10,
        )
    ) as connection:
        await connection.send(audio)
        await connection.sendSilence()
        await FileRenderer(connection).render()
        print("Done")
        await connection.stop()


asyncio.run(main())
