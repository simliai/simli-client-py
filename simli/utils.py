import av
import numpy as np


audioResampler = av.AudioResampler(format="s16", layout=1, rate=16000)


def load_audio_file(filePath: str) -> bytes:
    """
    Loads an audio file and returns the raw bytes. don't use this function for raw files.
    """
    contianer = av.open(filePath)
    audioBytes: list[bytes] = []
    for frame in contianer.decode(audio=0):
        for resampledFrame in audioResampler.resample(frame):
            audioBytes.append(resampledFrame.to_ndarray())
    audioBytes = (np.concatenate(audioBytes, axis=1)).astype(np.int16)
    return audioBytes.tobytes()


def load_audio_file_iter(filePath: str):
    """
    Loads an audio file and returns the raw bytes. don't use this function for raw files.
    """

    contianer = av.open(filePath)
    for frame in contianer.decode(audio=0):
        for resampledFrame in audioResampler.resample(frame):
            yield resampledFrame.to_ndarray().tobytes()
