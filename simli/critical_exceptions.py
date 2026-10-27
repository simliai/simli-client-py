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

    @classmethod
    def fromServerMessage(cls, message: str, status_code: int | None = None) -> "SimliExceptions":
        """
        The code an error message from the Simli server names, UNKNOWN_ERROR when it names none.
        A 402 is always a billing error, its message is free text.
        """
        if message.strip() in cls.__members__:
            return cls[message.strip()]
        if status_code == 402:
            return cls.BILLING_ERROR
        return cls.UNKNOWN_ERROR


class SimliError(Exception):
    """
    Base class of the errors SimliClient raises.

    Attributes:
        message (str): What failed, with the Simli server's own message when it sent one.
        code (SimliExceptions): The error code, UNKNOWN_ERROR when the server's message names none.
        retryable (bool): Whether SimliClient.start retries after this error.
    """

    retryable: bool = False

    def __init__(self, message: str, code: SimliExceptions = SimliExceptions.UNKNOWN_ERROR):
        super().__init__(message)
        self.message = message
        self.code = code


class SimliAPIError(SimliError):
    """
    An HTTP request to the Simli API failed, e.g. creating the session token with an invalid API
    key, an unknown face ID or no credits left. SimliClient.start retries only a 5xx.

    Attributes:
        status_code (int): The HTTP status code.
        detail (str): The server's message.
    """

    def __init__(
        self,
        message: str,
        status_code: int,
        detail: str,
        code: SimliExceptions = SimliExceptions.UNKNOWN_ERROR,
    ):
        super().__init__(message, code)
        self.status_code = status_code
        self.detail = detail
        self.retryable = status_code >= 500


class SimliSessionError(SimliError):
    """
    The Simli server refused to start the session, or ended it, with an error message, e.g. an
    invalid or expired session token, or the rate limit. Also a message the SDK doesn't know,
    sent while the session was starting: its code is UNKNOWN_ERROR and its message the server's
    text as is. Never retried.
    """


class SimliConnectionError(SimliError):
    """
    The network failed: a request or the websocket couldn't connect, timed out or dropped, the
    websocket closed before the session started, or joining the LiveKit room failed. Retried by
    SimliClient.start, which retries nothing else but a 5xx SimliAPIError.
    """

    retryable = True


__all__ = [
    "SimliAPIError",
    "SimliConnectionError",
    "SimliError",
    "SimliExceptions",
    "SimliSessionError",
]
