import asyncio
from simli import SimliClient, SimliConfig
import os
from dotenv import load_dotenv

load_dotenv(".env")


async def main():
    async with SimliClient(
        SimliConfig(
            os.getenv("SIMLI_API_KEY", ""),  # API Key
            os.getenv("SIMLI_FACE_ID", ""),  # Face ID
            maxSessionLength=5,
            maxIdleTime=10,
        )
    ) as connection:
        await connection.sendSilence()
        while connection.run:
            audioFrame = await connection.getNextAudioFrame()
            videoFrame = await connection.getNextVideoFrame()
            print(audioFrame, videoFrame)
        print("Done")
        await connection.stop()


asyncio.run(main())
