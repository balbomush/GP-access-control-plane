"""api_server exception types — moved from api_server.py (package split)."""


class RequestBodyTooLarge(ValueError):
    pass


class RuntimeBusyError(RuntimeError):
    pass
