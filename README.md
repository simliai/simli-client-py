# Simli AI SDK

This package is a simple and streamlined to use the Simli WebRTC API. To use the Simli SDK:

```bash
pip install simli-ai
```

The simplest way to use the package looks like this

```python
import asyncio
from simli import SimliClient, SimliConfig
from simli.renderers import FileRenderer
async def main():
    async with SimliClient(
        SimliConfig(
            apiKey="",  # API Key
            faceId="",  # Face ID
            maxSessionLength=20,
            maxIdleTime=10,
        )
    ) as connection:
        await connection.send(audio) # Audio is the raw PCM16 16khz mono audio data
        await FileRenderer(connection).render() # Write the output to an output.mp4 file

asyncio.run(main())
```

The audio can be loaded from a local file or a result of a TTS call.

If you want to display the stream, you can install the optional dependency "local"

```bash
pip install "simli-ai[local]"
```

And modify the snippet above by replacing `FileRenderer` with `LocalRenderer`. Note that LocalRenderer doesn't work on headless machines.

If you want to get the data in your python program to do extra processing on it later (or just want to access the data and send it somewhere else as you need) replace `FileRenderer` with `NPArrayRenderer`.

Now, calling `NPArrayRenderer().render()` returns two `np.ndarray` objects representing video and audio respectively. The shape of these objects will always be (N, H, W, 3) and (2, L) where N is the number of frames, H is the height of the frame, W is the width of the frame, 3 is the number of channels (always RGB) and for the audio 2 is the number of audio channels (always stereo) and L is the number of samples in the audio file
(frame rate is 30 sampling rate is 48000hz).

Note that you can use the SimliClient without the using statement. It requires that you add the following calls to your function

```python
connection = SimliClient(
        SimliConfig(
            apiKey="",  # API Key
            faceId="",  # Face ID
            maxSessionLength=20,
            maxIdleTime=10,
        )
    )
await connection.Initialize() # First thing after you create the SimliCLient
# The rest of your code
await connection.stop() # Call to gracefully shut down the SimliClient
```

## Accessing the data on your own

You might not want to use the Renderer utils but would like to access the frames on your own. There are two ways to do so:

1. Use `await SimliClient().getNextVideoFrame()` and `await SimliClient().getNextAudioFrame()` to receive the next frame and process them one by one
2. Use `async for videoFrame in SimliClient().getVideoStreamIterator()` and `async for audioFrame in SimliClient().getAudioStreamIterator()` to get the frames all in the same loop. Note that it is recommended to run these loops in `asyncio.create_task` to receive all the frames concurrently.

The video and audio frames are [PyAV](https://pyav.org/docs/stable/) frames.
