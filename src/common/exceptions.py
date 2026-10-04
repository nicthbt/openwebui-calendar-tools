class CustomToolException(Exception):
    def __init__(self, message, error=None):
        super().__init__(message)
        self.error = error


class CalendarException(CustomToolException):
    pass


class OpenTerminalException(CustomToolException):
    pass
