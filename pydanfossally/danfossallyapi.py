"""Async communication layer for the Danfoss Ally API."""

from __future__ import annotations

import asyncio
import base64
import datetime
from importlib import metadata
import json
import logging
from typing import Any

import httpx

from .exceptions import (
    APIError,
    BadRequestError,
    ForbiddenError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
    UnauthorizedError,
    UnexpectedError,
)

API_HOST = "https://api.danfoss.com"
TOKEN_PATH = "/oauth2/token"
ALLY_PREFIX = "/ally"
DEFAULT_TIMEOUT = 10.0

_LOGGER = logging.getLogger(__name__)

_MODE_TO_LAST_CLICK_TIME_FORMAT_MAP = {
    # The format was reverse engineered from experimentation with the API and the Danfoss Ally app.
    "at_home": "010000",
    "leaving_home": "000101",
}
"""Map mode to obscure format required by `last_click_time`."""

_PACKAGE_NAME = "pydanfossally"
_DEFAULT_USER_AGENT_SUFFIX = f"{_PACKAGE_NAME}/unknown"


def _resolve_user_agent_suffix() -> str:
    """Resolve the library user agent suffix from installed package metadata."""
    try:
        version = metadata.version(_PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        version = "unknown"
    return f"{_PACKAGE_NAME}/{version}"


class DanfossAllyAPI:
    """Async-first low-level Danfoss Ally API client."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent_prefix: str | None = None,
    ) -> None:
        """Initialize the API client."""
        self._key = ""
        self._secret = ""
        self._token = ""
        self._refresh_at = datetime.datetime.min
        self._owns_client = client is None
        self._timeout = timeout
        self._client = client
        self._user_agent_prefix = user_agent_prefix
        self._user_agent_suffix = _DEFAULT_USER_AGENT_SUFFIX

    async def __aenter__(self) -> DanfossAllyAPI:
        """Allow async context manager usage."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the owned HTTP client."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying async HTTP client when owned by this instance."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _async_get_client(self) -> httpx.AsyncClient:
        """Create the default async HTTP client on first use."""
        if self._client is None:
            self._client = await asyncio.to_thread(
                httpx.AsyncClient,
                base_url=API_HOST,
                timeout=self._timeout,
                headers={
                    "Accept": "application/json",
                    "User-Agent": self._user_agent,
                },
            )
        return self._client

    async def _ensure_user_agent(self) -> None:
        """Resolve the full user agent lazily inside an async code path."""
        if self._user_agent_suffix != _DEFAULT_USER_AGENT_SUFFIX:
            return
        self._user_agent_suffix = await asyncio.to_thread(_resolve_user_agent_suffix)

    @property
    def _user_agent(self) -> str:
        """Build the outgoing user agent string."""
        if self._user_agent_prefix:
            return f"{self._user_agent_prefix} {self._user_agent_suffix}"
        return self._user_agent_suffix

    def _generate_base64_token(self, key: str, secret: str) -> str:
        """Generate a base64 encoded client credential token."""
        key_secret_bytes = f"{key}:{secret}".encode("ascii")
        return base64.b64encode(key_secret_bytes).decode("ascii")

    def _auth_headers(self) -> dict[str, str]:
        """Build bearer auth headers for API requests."""
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": self._user_agent,
        }

    def _basic_auth_headers(self, token: str) -> dict[str, str]:
        """Build basic auth headers for OAuth token requests."""
        return {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {token}",
            "User-Agent": self._user_agent,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        skip_auth_refresh: bool = False,
    ) -> dict[str, Any]:
        """Execute one HTTP request and map API errors to project exceptions."""
        await self._ensure_user_agent()
        if not skip_auth_refresh:
            await self._refresh_token()

        request_headers = headers or self._auth_headers()
        client = await self._async_get_client()

        try:
            response = await client.request(
                method,
                path,
                json=payload,
                headers=request_headers,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as err:
            raise self._map_http_error(err) from err
        except httpx.TimeoutException as err:
            raise TimeoutError from err
        except httpx.ConnectError as err:
            raise ConnectionError from err
        except httpx.HTTPError as err:
            raise UnexpectedError from err

        if not response.content:
            return {}

        try:
            return response.json()
        except json.JSONDecodeError as err:
            raise UnexpectedError from err

    def _map_http_error(self, err: httpx.HTTPStatusError) -> Exception:
        """Translate HTTP failures into domain-specific exceptions."""
        status_code = err.response.status_code
        if status_code == 400:
            return BadRequestError()
        if status_code == 401:
            return UnauthorizedError()
        if status_code == 403:
            return ForbiddenError()
        if status_code == 404:
            return NotFoundError()
        if status_code == 429:
            return RateLimitError()
        if 500 <= status_code <= 599:
            return InternalServerError()
        return APIError(f"Unexpected HTTP status code: {status_code}")

    async def _refresh_token(self) -> bool:
        """Refresh OAuth2 token when it has expired."""
        if self._refresh_at > datetime.datetime.now():
            return False

        return await self.get_token()

    async def get_token(
        self, key: str | None = None, secret: str | None = None
    ) -> bool:
        """Retrieve and cache an OAuth2 access token."""
        await self._ensure_user_agent()
        if key is not None:
            self._key = key
        if secret is not None:
            self._secret = secret

        if not self._key or not self._secret:
            return False

        base64_token = self._generate_base64_token(self._key, self._secret)
        post_data = "grant_type=client_credentials"
        client = await self._async_get_client()

        try:
            req = await client.post(
                TOKEN_PATH,
                content=post_data,
                headers=self._basic_auth_headers(base64_token),
            )
            req.raise_for_status()
            response = req.json()
        except httpx.HTTPStatusError as err:
            mapped_error = self._map_http_error(err)
            if isinstance(mapped_error, (BadRequestError, UnauthorizedError)):
                _LOGGER.warning("Token request rejected by Danfoss Ally API")
                return False
            raise mapped_error from err
        except httpx.TimeoutException:
            _LOGGER.warning("Timeout communication with Danfoss Ally API")
            return False
        except httpx.HTTPError:
            _LOGGER.warning(
                "Unexpected error occurred while requesting Danfoss Ally token"
            )
            return False
        except json.JSONDecodeError:
            _LOGGER.warning("Bad request while requesting Danfoss Ally token")
            return False

        if "access_token" not in response or "expires_in" not in response:
            return False

        expires_in = float(response["expires_in"])
        self._refresh_at = datetime.datetime.now() + datetime.timedelta(
            seconds=expires_in - 30
        )
        self._token = response["access_token"]
        return True

    async def get_devices(self) -> dict[str, Any]:
        """Get the list of all devices."""
        return await self._request("GET", f"{ALLY_PREFIX}/devices")

    async def get_device(self, device_id: str) -> dict[str, Any]:
        """Get one device."""
        return await self._request("GET", f"{ALLY_PREFIX}/devices/{device_id}")

    async def get_device_sub_devices(self, device_id: str) -> dict[str, Any]:
        """Get the list of sub-devices under one gateway device."""
        return await self._request(
            "GET",
            f"{ALLY_PREFIX}/devices/{device_id}/sub-devices",
        )

    async def get_device_status(self, device_id: str) -> dict[str, Any]:
        """Get the latest device status entries."""
        return await self._request(
            "GET",
            f"{ALLY_PREFIX}/devices/{device_id}/status",
        )

    async def send_command(
        self,
        device_id: str,
        listofcommands: list[tuple[str, Any]],
    ) -> bool:
        """Send generic commands to one device."""
        request_body = {
            "commands": [
                {"code": code, "value": value} for code, value in listofcommands
            ]
        }
        _LOGGER.debug("Sending command to device %s: %s", device_id, request_body)
        response = await self._request(
            "POST",
            f"{ALLY_PREFIX}/devices/{device_id}/commands",
            payload=request_body,
        )
        return self._command_response_to_bool(response)

    async def set_temperature(
        self,
        device_id: str,
        temp: int,
        code: str = "manual_mode_fast",
    ) -> bool:
        """Set the device target temperature using one command code."""
        return await self.send_command(device_id, [(code, temp)])

    async def set_mode(self, device_id: str, mode: str) -> bool:
        """Set the device operating mode."""
        commands: list[tuple[str, Any]] = [("mode", mode)]
        if mode in _MODE_TO_LAST_CLICK_TIME_FORMAT_MAP:
            commands.append(
                (
                    "last_click_time",
                    f"{datetime.datetime.now():%Y%m%d%H%M}"
                    f"{_MODE_TO_LAST_CLICK_TIME_FORMAT_MAP[mode]}",
                )
            )
        return await self.send_command(device_id, commands)

    def _command_response_to_bool(self, response: dict[str, Any]) -> bool:
        """Support both documented and real-world command response shapes."""
        if "result" in response:
            return bool(response["result"])
        if "t" in response:
            return True
        return False

    @property
    def token(self) -> str:
        """Return the cached OAuth token."""
        return self._token
