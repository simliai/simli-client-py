import asyncio
import time
import threading

from . import SimliClient

try:
    from daily import *  # noqa: F403
except ImportError:
    raise ImportError(
        "daily is required for DailyRenderer, Install optional dependencies using \n\"pip install 'simli-ai[daily]'\""
    )

Daily.init()  # noqa: F405


class DailyRenderer:
    def __init__(
        self,
        client: SimliClient,
        meeting_url: str,
        dailyClient: CallClient,  # noqa: F405
        meeting_token: str = None,
        videoWidth=512,
        videoHeight=512,
        client_settings: dict = None,
    ):
        """
        If supplying your own client_settings, make sure to include
        "inputs": {
            "camera": {
                "isEnabled": True,
                "settings": {"deviceId": "Camera"},
            },
            "microphone": {
                "isEnabled": True,
                "settings": {"deviceId": "Microphone"},
            },
        }
        """
        self.simliClient = client
        self.microphone = Daily.create_microphone_device(  # noqa: F405
            "Microphone",
            sample_rate=48000,
            channels=2,  # non_blocking=True
        )
        self.camera = Daily.create_camera_device(  # noqa: F405
            "Camera",
            videoWidth,
            videoHeight,
            color_format="RGB",
        )
        self.dailyClient = dailyClient
        self.dailyClient.update_subscription_profiles(
            {"base": {"camera": "unsubscribed", "microphone": "subscribed"}}
        )

        if client_settings is None:
            self.dailyClient.join(
                meeting_url,
                meeting_token=meeting_token,
                client_settings={
                    "inputs": {
                        "camera": {
                            "isEnabled": True,
                            "settings": {"deviceId": "Camera"},
                        },
                        "microphone": {
                            "isEnabled": True,
                            "settings": {"deviceId": "Microphone"},
                        },
                    }
                },
                completion=self.on_join,
            )
        else:
            self.dailyClient.join(
                meeting_url,
                client_settings=client_settings,
                completion=self.on_join,
            )
        self.running = False
        self.videoBuffer = []
        self.audioBuffer = []

    def on_join(self, data, error):
        self.running = True
        print("Started")

    async def render(self):
        while not self.running:
            await asyncio.sleep(0.01)
        videoTask = asyncio.create_task(self.getVideo())
        audioTask = asyncio.create_task(self.getAudio())
        videoPublishTask = threading.Thread(target=self.publishVideo)
        audioPublishTask = threading.Thread(target=self.publishAudio)
        videoPublishTask.start()
        audioPublishTask.start()
        await asyncio.gather(audioTask, videoTask)
        videoPublishTask.join()
        audioPublishTask.join()
        self.dailyClient.leave()

    async def getVideo(self):
        async for frame in self.simliClient.getVideoStreamIterator("rgb24"):
            if frame is None:
                self.videoBuffer.append(None)
                break
            self.videoBuffer.append(frame.to_ndarray().tobytes())

    async def getAudio(self):
        async for frame in self.simliClient.getAudioStreamIterator():
            if frame is None:
                self.audioBuffer.append(None)
                break
            self.audioBuffer.append(frame.to_ndarray().tobytes())

    def publishVideo(self):
        while not self.running or len(self.videoBuffer) == 0:
            time.sleep(0.01)
        s = time.time()
        while self.running:
            try:
                frame = self.videoBuffer.pop(0)
            except IndexError:
                break
            if frame is None:
                break
            self.camera.write_frame(frame)
            time.sleep((1 / 30) - (time.time() - s))
            s = time.time()

    def publishAudio(self):
        while not self.running or len(self.audioBuffer) == 0:
            time.sleep(0.01)
        # s = time.time()
        while self.running:
            try:
                frame = self.audioBuffer.pop(0)
            except IndexError:
                break
            if frame is None:
                break
            self.microphone.write_frames(frame)
            # time.sleep(0.04 - (time.time() - s))
            # s = time.time()
