"""Exceptions for Danfoss Ally."""

from http.client import HTTPException


class NotFoundError(HTTPException):
    ...


class InternalServerError(HTTPException):
    ...


class UnauthorizedError(HTTPException):
    ...


class UnexpectedError(Exception):
    ...
