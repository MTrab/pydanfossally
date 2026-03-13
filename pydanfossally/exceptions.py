"""Exceptions for Danfoss Ally."""

from http.client import HTTPException


class APIError(HTTPException):
    """Base exception for HTTP errors returned by the Ally API."""


class BadRequestError(APIError):
    """Raised when the API rejects the request payload."""


class RateLimitError(APIError):
    """Raised when the API throttles requests."""


class NotFoundError(APIError):
    """Raised when a resource cannot be found."""


class InternalServerError(APIError):
    ...


class UnauthorizedError(APIError):
    ...


class UnexpectedError(Exception):
    ...
