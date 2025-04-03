import asyncio
from simli import SimliClient, SimliConfig

from simli.livekit_renderer import LivekitRenderer
from livekit import rtc, api

# from simli.renderers import FileRenderer
import os
from dotenv import load_dotenv
import time
# from logging import getLogger

# getLogger().setLevel("DEBUG")
load_dotenv(".env", override=True)

with open("test_audio.raw", "rb") as f:
    audio = f.read()


async def main():
    connection = SimliClient(
        SimliConfig(
            os.getenv("SIMLI_API_KEY", ""),  # API Key
            os.getenv("SIMLI_FACE_ID", ""),  # Face ID
            maxSessionLength=300,
            maxIdleTime=300,
        ),
        # useTrunServer=True,
        # simliURL="http://localhost:8892",
        latencyInterval=0,
    )
    livekitURL = os.getenv("LIVEKIT_URL", "")
    livekitToken = (
        api.AccessToken(
            os.getenv("LIVEKIT_API_KEY", ""),
            os.getenv("LIVEKIT_API_SECRET", ""),
        )
        .with_identity("python-publisher")
        .with_name("Python Publisher")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=os.getenv("LIVEKIT_ROOM"),
            )
        )
        .to_jwt()
    )

    livekitRenderer = LivekitRenderer(
        connection,
        livekitURL,
        livekitToken,
        rtc.Room(loop=asyncio.get_running_loop()),
    )
    s = time.time()
    await asyncio.gather(livekitRenderer.InitLivekit(), connection.Initialize())
    print(time.time() - s)
    renderTask = asyncio.create_task(livekitRenderer.render())

    for i in range(1):
        await connection.send(audio)
        await asyncio.sleep(len(audio) / 32000 + 5)
    await connection.send(b"DONE")
    print("Done")
    await renderTask
    await connection.stop()


asyncio.run(main())
