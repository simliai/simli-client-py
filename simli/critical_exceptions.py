from enum import StrEnum, auto


class SimliExceptions(StrEnum):
    INVALID_API_KEY = auto()
    BILLING_ERROR = auto()
    MISSING_BILLING_INFO = auto()
    INVALID_FACE_ID = auto()
    INVALID_EMOTION = auto()
    UNKNOWN_ERROR = auto()

    @classmethod
    def _missing_(cls, value: object):
        if not isinstance(value, str) or len(value := value.strip()) == 0:
            raise ValueError()
        match value:
            case "INVALID_API_KEY":
                return cls.INVALID_API_KEY
            case "BILLING_ERROR":
                return cls.BILLING_ERROR
            case "MISSING_BILLING_INFO":
                return cls.MISSING_BILLING_INFO
            case "INVALID_FACE_ID":
                return cls.INVALID_FACE_ID
            case "INVALID_EMOTION":
                return cls.INVALID_EMOTION
            case _:
                return cls.BILLING_ERROR
