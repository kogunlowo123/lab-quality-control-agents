"""Domain errors mapped to HTTP status codes by the API layer."""

from __future__ import annotations


class QcError(Exception):
    code = "invalid_request"
    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(QcError):
    code = "not_found"
    status = 404


class ConflictError(QcError):
    code = "conflict"
    status = 409


class InvalidStateError(QcError):
    code = "invalid_state"
    status = 409


class MessageFormatError(QcError):
    """An HL7 v2 or ASTM E1394 message could not be parsed."""

    code = "invalid_message"
    status = 400
    ack: str | None = None  # HL7 acknowledgement returned to the sender, when applicable


class InsufficientDataError(QcError):
    code = "insufficient_data"
    status = 422


class UnauthorizedError(QcError):
    code = "unauthorized"
    status = 401


class ForbiddenError(QcError):
    code = "forbidden"
    status = 403


class ApprovalRequiredError(QcError):
    """Policy returned require_approval: a qualified human must perform the action."""

    code = "approval_required"
    status = 403


class SeparationOfDutiesError(QcError):
    code = "separation_of_duties"
    status = 403
