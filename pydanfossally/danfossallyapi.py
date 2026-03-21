"""Async communication layer for the Danfoss Ally API."""

from __future__ import annotations

import asyncio
import base64
from collections import defaultdict, deque
import datetime
from importlib import metadata
import json
import logging
import re
import time
from typing import Any

import aiohttp

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
DEFAULT_REQUEST_RATE_LIMIT = 3.0
DIAGNOSTIC_WINDOW_SECONDS = 3600.0

_LOGGER = logging.getLogger(__name__)

_MODE_TO_LAST_CLICK_TIME_FORMAT_MAP = {
    # The format was reverse engineered from experimentation with the API and the Danfoss Ally app.
    "at_home": "010000",
    "leaving_home": "000101",
}
"""Map mode to obscure format required by `last_click_time`."""

_PACKAGE_NAME = "pydanfossally"
_DEFAULT_USER_AGENT_SUFFIX = f"{_PACKAGE_NAME}/unknown"
_DEVICE_PATH_PATTERN = re.compile(r"^/ally/devices/[^/]+")


def _resolve_user_agent_suffix() -> str:
    """Resolve the library user agent suffix from installed package metadata."""
    try:
        version = metadata.version(_PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        version = "unknown"
    return f"{_PACKAGE_NAME}/{version}"


def _normalize_endpoint_path(path: str) -> str:
    """Normalize dynamic device paths into stable endpoint names."""
    if path == TOKEN_PATH:
        return path
    if path.startswith(f"{ALLY_PREFIX}/devices/"):
        return _DEVICE_PATH_PATTERN.sub(f"{ALLY_PREFIX}/devices/{{device_id}}", path)
    return path


class DanfossAllyAPI:
    """Async-first low-level Danfoss Ally API client."""

    def __init__(
        self,
        client: aiohttp.ClientSession | None = None,
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
        self._request_rate_limit: float | None = DEFAULT_REQUEST_RATE_LIMIT
        self._request_rate_lock = asyncio.Lock()
        self._next_request_slot = 0.0
        self._client = client
        self._user_agent_prefix = user_agent_prefix
        self._user_agent_suffix = _DEFAULT_USER_AGENT_SUFFIX
        self._diagnostic_window = DIAGNOSTIC_WINDOW_SECONDS
        self._endpoint_request_times: dict[str, deque[float]] = defaultdict(deque)

    async def __aenter__(self) -> DanfossAllyAPI:
        """Allow async context manager usage."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the owned HTTP client."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying async HTTP client when owned by this instance."""
        if self._owns_client and self._client is not None:
            await self._client.close()

    async def _async_get_client(self) -> aiohttp.ClientSession:
        """Create the default async HTTP client on first use."""
        if self._client is None:
            _LOGGER.debug(
                "Creating async HTTP client for %s with timeout=%s",
                API_HOST,
                self._timeout,
            )
            self._client = aiohttp.ClientSession(
                base_url=API_HOST,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
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
        _LOGGER.debug("Resolved User-Agent suffix to %s", self._user_agent_suffix)

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
        await self._wait_for_request_slot()
        self._record_request(path)
        _LOGGER.debug(
            "Sending %s %s (payload=%s, auth_refresh=%s)",
            method,
            path,
            "yes" if payload is not None else "no",
            not skip_auth_refresh,
        )

        try:
            async with client.request(
                method,
                path,
                json=payload,
                headers=request_headers,
            ) as response:
                status_code = response.status
                body = await response.read()
        except asyncio.TimeoutError as err:
            _LOGGER.debug("Timeout while calling %s %s", method, path)
            raise TimeoutError from err
        except aiohttp.ClientConnectionError as err:
            _LOGGER.debug("Connection error while calling %s %s", method, path)
            raise ConnectionError from err
        except aiohttp.ClientError as err:
            _LOGGER.debug(
                "Unexpected HTTP error while calling %s %s: %s", method, path, err
            )
            raise UnexpectedError from err

        if status_code >= 400:
            _LOGGER.debug(
                "HTTP error for %s %s: status=%s",
                method,
                path,
                status_code,
            )
            raise self._map_http_error(status_code, path)

        _LOGGER.debug(
            "Received %s response for %s %s",
            status_code,
            method,
            path,
        )

        if not body:
            _LOGGER.debug("Empty response body for %s %s", method, path)
            return {}

        try:
            data = json.loads(body)
            _LOGGER.debug(
                "Decoded JSON response for %s %s with keys=%s",
                method,
                path,
                sorted(data.keys()),
            )
            return data
        except json.JSONDecodeError as err:
            _LOGGER.debug("Failed to decode JSON response for %s %s", method, path)
            raise UnexpectedError from err

    async def _wait_for_request_slot(self) -> None:
        """Throttle outbound requests so the API does not receive bursts."""
        if self._request_rate_limit is None:
            return

        loop = asyncio.get_running_loop()
        min_interval = 1 / self._request_rate_limit

        async with self._request_rate_lock:
            now = loop.time()
            scheduled_at = max(now, self._next_request_slot)
            self._next_request_slot = scheduled_at + min_interval

        delay = scheduled_at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)

    def _map_http_error(self, status_code: int, path: str) -> Exception:
        """Translate HTTP failures into domain-specific exceptions."""
        if status_code == 400:
            mapped = BadRequestError()
        elif status_code == 401:
            mapped = UnauthorizedError()
        elif status_code == 403:
            mapped = ForbiddenError()
        elif status_code == 404:
            mapped = NotFoundError()
        elif status_code == 429:
            mapped = RateLimitError()
        elif 500 <= status_code <= 599:
            mapped = InternalServerError()
        else:
            mapped = APIError(f"Unexpected HTTP status code: {status_code}")
        _LOGGER.debug(
            "Mapped HTTP status %s on %s to %s",
            status_code,
            path,
            mapped.__class__.__name__,
        )
        return mapped

    def _record_request(self, path: str) -> None:
        """Count requests by normalized endpoint path."""
        endpoint = _normalize_endpoint_path(path)
        timestamps = self._endpoint_request_times[endpoint]
        timestamps.append(time.monotonic())
        self._prune_timestamps(timestamps)

    def _prune_timestamps(self, timestamps: deque[float]) -> None:
        """Drop diagnostics entries that fall outside the rolling window."""
        cutoff = time.monotonic() - self._diagnostic_window
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

    async def _refresh_token(self) -> bool:
        """Refresh OAuth2 token when it has expired."""
        if self._refresh_at > datetime.datetime.now():
            _LOGGER.debug("Skipping token refresh; cached token is still valid")
            return False

        _LOGGER.debug("Refreshing OAuth token")
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
            _LOGGER.debug("Token request skipped because credentials are missing")
            return False

        base64_token = self._generate_base64_token(self._key, self._secret)
        post_data = "grant_type=client_credentials"
        client = await self._async_get_client()
        await self._wait_for_request_slot()
        self._record_request(TOKEN_PATH)

        try:
            async with client.post(
                TOKEN_PATH,
                data=post_data,
                headers=self._basic_auth_headers(base64_token),
            ) as response:
                status_code = response.status
                body = await response.read()
        except asyncio.TimeoutError:
            _LOGGER.warning("Timeout communication with Danfoss Ally API")
            return False
        except aiohttp.ClientError:
            _LOGGER.warning(
                "Unexpected error occurred while requesting Danfoss Ally token"
            )
            return False

        if status_code >= 400:
            mapped_error = self._map_http_error(status_code, TOKEN_PATH)
            if isinstance(mapped_error, (BadRequestError, UnauthorizedError)):
                _LOGGER.warning("Token request rejected by Danfoss Ally API")
                return False
            raise mapped_error

        try:
            response_data = json.loads(body)
        except json.JSONDecodeError:
            _LOGGER.warning("Bad request while requesting Danfoss Ally token")
            return False

        _LOGGER.debug("Token request succeeded with status %s", status_code)

        if "access_token" not in response_data or "expires_in" not in response_data:
            return False

        expires_in = float(response_data["expires_in"])
        self._refresh_at = datetime.datetime.now() + datetime.timedelta(
            seconds=expires_in - 30
        )
        self._token = response_data["access_token"]
        _LOGGER.debug("Stored OAuth token with expires_in=%s seconds", expires_in)
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
        _LOGGER.debug(
            "Sending %s command(s) to device %s with codes=%s",
            len(listofcommands),
            device_id,
            [code for code, _ in listofcommands],
        )
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

    def get_diagnostics(self) -> dict[str, dict[str, int]]:
        """Return lightweight API diagnostics for debugging and tuning."""
        request_counts: dict[str, int] = {}
        for endpoint, timestamps in self._endpoint_request_times.items():
            self._prune_timestamps(timestamps)
            if timestamps:
                request_counts[endpoint] = len(timestamps)
        return {
            "request_counts": dict(sorted(request_counts.items())),
        }
