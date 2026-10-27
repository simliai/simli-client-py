import json
from enum import StrEnum, auto

from .logger import logger


class SimliEvent(StrEnum):
    START = auto()
    STOP = auto()
    ERROR = auto()
    SILENT = auto()
    SPEAK = auto()
    CONNECTION_INFO = auto()
    VIDEO_META = auto()
    DESTINATION = auto()
    UNKNOWN = auto()
    ACK = auto()

    @classmethod
    def _missing_(cls, value: object):
        if not isinstance(value, str) or len(value := value.strip()) == 0:
            raise ValueError()
        if value.startswith("{"):
            return cls.fromJSON(value)
        # The server's errors come as "ERROR: <message>"
        value = value.upper().split()[0].rstrip(":")
        match value:
            case "ACK":
                return cls.ACK
            case "START":
                return cls.START
            case "CLOSING":
                return cls.STOP
            case "STOP":
                return cls.STOP
            case "ERROR":
                return cls.ERROR
            case "RATE":
                return cls.ERROR
            case "SILENT":
                return cls.SILENT
            case "SPEAK":
                return cls.SPEAK
            case _:
                if "SDP" in value or "LIVEKIT" in value:
                    return cls.CONNECTION_INFO
                elif "VIDEO_METADATA" in value:
                    return cls.VIDEO_META
                elif "DESTINATION" in value:
                    return cls.DESTINATION
                elif "ENDFRAME" in value:
                    logger.info(value)
                    return cls.STOP
                else:
                    return cls.UNKNOWN

    @classmethod
    def fromJSON(cls, message: str) -> "SimliEvent":
        """
        The event a JSON message from the server is, by its keys: the server sends it without
        spaces, so the worker id in a destination could read as anything else
        """
        try:
            keys = json.loads(message).keys()
        except (ValueError, AttributeError):
            return cls.UNKNOWN
        if "sdp" in keys or "livekit_url" in keys:
            return cls.CONNECTION_INFO
        if "video_metadata" in keys:
            return cls.VIDEO_META
        if "destination" in keys:
            return cls.DESTINATION
        return cls.UNKNOWN
