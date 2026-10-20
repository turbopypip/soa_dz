class ApiError(Exception):
    def __init__(self, status, code, message, details=None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def fail(status, code, message, details=None):
    raise ApiError(status, code, message, details)


def invalid(field, message):
    fail(
        400,
        "VALIDATION_ERROR",
        "Invalid request",
        {"violations": [{"field": field, "message": message}]},
    )
