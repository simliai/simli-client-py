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
        value = value.upper().split()[0]
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
