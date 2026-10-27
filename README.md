# Simli AI SDK

This package is a simple and streamlined to use the Simli WebRTC API. To use the Simli SDK:

```bash
pip install simli-ai
```

The simplest way to use the package looks like this

```python
import asyncio
from simli import SimliClient, SimliConfig
from simli.renderers.renderers import FileRenderer


async def main():
    async with SimliClient(
        api_key="",  # API Key
        config=SimliConfig(
            faceId="bf13113a-355a-4d97-8b90-4edccd1da7b2",  # Face ID
            maxSessionLength=120,
            maxIdleTime=60,
        ),
    ) as connection:
        await connection.send(audio)  # Audio is the raw PCM16 16khz mono audio data
        await FileRenderer(connection).render()  # Write the output to an output.mp4 file


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
    api_key="",  # API Key
    config=SimliConfig(
        faceId="bf13113a-355a-4d97-8b90-4edccd1da7b2",  # Face ID
        maxSessionLength=120,
        maxIdleTime=60,
    ),
)
await connection.start()  # First thing after you create the SimliClient
# The rest of your code
await connection.stop()  # Call to gracefully shut down the SimliClient
```

## Errors

`start()` raises as soon as Simli reports a problem, with the server's message:

- `SimliAPIError`: an HTTP request to the Simli API failed, e.g. an invalid API key, an unknown face ID or no credits left. `status_code` and `detail` hold the HTTP status and the server's message.
- `SimliSessionError`: the server refused to start the session, e.g. an invalid or expired session token, or the rate limit. Also a message from the server that the SDK doesn't know, sent while the session is starting: its `code` is `SimliExceptions.UNKNOWN_ERROR`, its `message` is the server's text as is, and it is logged as `Unknown error from Simli: <message>`.
- `SimliConnectionError`: the network failed, e.g. a request or the websocket couldn't connect, timed out or dropped, or joining the LiveKit room failed.

`start()` retries only failures known to be transient: a `SimliConnectionError` or a 5xx response, up to `retry_count` tries in all. It backs off exponentially, waiting 1, 2, 4, 8 and then 16 seconds before each further retry, each plus up to 20% at random. Everything else raises straight away, without retrying. (`retry_timeout` is deprecated and ignored.)

All of them are `SimliError`s, whose `code` names the error when the server sent one (e.g. `SimliExceptions.INVALID_FACE_ID`). Once the session has started, a message the SDK doesn't know no longer ends it: it is logged as a warning on the `simli_client` logger.

Once the session has ended, `connection.stopReason` holds the reason the server gave (e.g. `"IDLE"` or `"MAX"`) and `connection.error` the error that ended it, if one did. `send()` then raises that error, or a `SimliConnectionError` naming the reason.

```python
from simli import SimliAPIError, SimliError

try:
    await connection.start()
except SimliAPIError as e:
    print(e.status_code, e.detail)
except SimliError as e:
    print(e)
```

## Camera, background and foreground (Trinity-2 faces)

A Trinity-2 avatar's camera, background and foreground can be changed while the session runs. `SimliConfig(camera=..., background_id=..., foreground_id=...)` sets the ones it starts with.

- `await connection.setCamera(camera: CameraSetting | None, *, lerp_seconds: float | None = None)` replaces the whole camera, the starting one included. A `CameraSetting`'s values are offsets from the avatar's own framing, not from the previous call, and the fields you leave out stay neutral. `None` goes back to the avatar's own framing. With `lerp_seconds` the camera glides there over that many seconds, from wherever it is now; without it (or with 0 or less) it cuts.
- `await connection.setBackground(background_id: str | None)` puts one of your uploaded backgrounds behind the avatar, by the id `POST /assets/backgrounds` returned. `None` goes back to the avatar's own.
- `await connection.setForeground(foreground_id: str | None)` lays one of your uploaded foregrounds (`POST /assets/foregrounds`) over the avatar. `None` takes it away.

A new background or foreground shows a moment later, once it has loaded, and the last one you send wins.

| `CameraSetting` field | Meaning | Supported range |
|---|---|---|
| `pos_x_off` | Moves the avatar right, in half canvas widths | -10 to 10 |
| `pos_y_off` | Moves the avatar down, in half canvas heights | -10 to 10 |
| `pos_z_off` | Metres the camera backs away from the avatar (negative moves in) | -5 to 5 |
| `zoom` | Multiplies the avatar's zoom; 1 is no change | above 0, up to 4 |
| `rot_x_off`, `rot_y_off`, `rot_z_off` | Degrees the camera tilts up, pans right, rolls clockwise | -180 to 180 |
| `orbit_x_off` | Degrees the camera orbits up over the avatar's head | -80 to 80 |
| `orbit_y_off` | Degrees the camera orbits round to its right | -180 to 180 |

```python
from simli import CameraSetting, SimliClient, SimliConfig

async with SimliClient(
    api_key="",  # API Key
    config=SimliConfig(faceId="bf13113a-355a-4d97-8b90-4edccd1da7b2"),
) as connection:
    # Glide in and round over 2 seconds
    await connection.setCamera(CameraSetting(zoom=1.5, orbit_y_off=20), lerp_seconds=2)
    # One of your uploaded backgrounds, by the id POST /assets/backgrounds returned
    await connection.setBackground("6f1c2a9e-1b2c-4d3e-8f90-0123456789ab")
    ...
    await connection.setCamera(None)  # back to the avatar's own framing, at once
    await connection.setBackground(None)  # and its own background
```

A value outside its supported range is still applied. The server's warning about it, and its refusals (an id that isn't one of your uploads, a face that isn't Trinity-2), are logged as warnings on the `simli_client` logger, and none of them end the session. The methods raise `ValueError` without sending anything for a NaN or infinite camera value or `lerp_seconds`, or an id that isn't the 36 character one the upload returned. Once the session has ended they raise as `send()` does.

## Accessing the data on your own

You might not want to use the Renderer utils but would like to access the frames on your own. There are two ways to do so:

1. Use `await SimliClient().getNextVideoFrame()` and `await SimliClient().getNextAudioFrame()` to receive the next frame and process them one by one
2. Use `async for videoFrame in SimliClient().getVideoStreamIterator()` and `async for audioFrame in SimliClient().getAudioStreamIterator()` to get the frames all in the same loop. Note that it is recommended to run these loops in `asyncio.create_task` to receive all the frames concurrently.

The video and audio frames are [PyAV](https://pyav.org/docs/stable/) frames.
