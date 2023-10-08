"""Doing the communications in this file."""
from __future__ import annotations

import base64
import datetime
import json
import logging

import requests

from .exceptions import (
    InternalServerError,
    NotFoundError,
    UnauthorizedError,
    UnexpectedError,
)

API_HOST = "https://api.danfoss.com"

_LOGGER = logging.getLogger(__name__)

_MODE_TO_LAST_CLICK_TIME_FORMAT_MAP = {
    # The format was reverse engineered from experimentation with the API and the Danfoss Ally app
    "at_home": "010000",
    "leaving_home": "000101",
}
"""Map mode to obscure format required by `last_click_time`"""


class DanfossAllyAPI:
    def __init__(self) -> None:
        """Init API."""
        self._key = ""
        self._secret = ""
        self._token = ""
        self._refresh_at = datetime.datetime.now()

    def _call(
        self, path: str, headers_data: str | None = None, payload: str | None = None
    ) -> dict:
        """Do the actual API call async."""
        self._refresh_token()

        if isinstance(headers_data, type(None)):
            headers_data = self.__headerdata()

        try:
            if payload:
                _LOGGER.debug("Send command: %s: %s", path, json.dumps(payload))
                req = requests.post(
                    API_HOST + path, json=payload, headers=headers_data, timeout=10
                )
            else:
                req = requests.get(API_HOST + path, headers=headers_data, timeout=10)

            req.raise_for_status()
        except requests.exceptions.HTTPError as err:
            code = err.response.status_code
            if payload:
                _LOGGER.debug("Http status code: %s", code)
            if code == 401:
                raise UnauthorizedError from err
            if code == 404:
                raise NotFoundError from err
            if code == 500:
                raise InternalServerError from err
            return False
        except TimeoutError as err:
            raise TimeoutError from err
        except requests.exceptions.ConnectionError as err:
            raise ConnectionError from err
        except:
            raise UnexpectedError

        response = req.json()
        if payload:
            _LOGGER.debug("Command response: %s", response)
        print("JSON: ", response)
        return response

    def _refresh_token(self) -> bool:
        """Refresh OAuth2 token if expired."""
        if self._refresh_at > datetime.datetime.now():
            return False

        return self.getToken()

    def _generate_base64_token(self, key: str, secret: str) -> str:
        """Generates a base64 token"""
        key_secret = key + ":" + secret
        key_secret_bytes = key_secret.encode("ascii")
        base64_bytes = base64.b64encode(key_secret_bytes)
        base64_token = base64_bytes.decode("ascii")

        return base64_token

    def __headerdata(self, token: str | None = None) -> dict:
        """Generate header data."""
        headers = {
            "Accept": "application/json",
        }
        if isinstance(token, type(None)):
            headers.update({"Authorization": f"Bearer {self._token}"})
        else:
            headers.update({"Content-Type": "application/x-www-form-urlencoded"})
            headers.update({"Authorization": f"Basic {token}"})

        return headers

    def getToken(self, key: str | None = None, secret: str | None = None) -> bool:
        """Get token."""

        if not isinstance(key, type(None)):
            self._key = key
        if not isinstance(secret, type(None)):
            self._secret = secret

        base64_token = self._generate_base64_token(self._key, self._secret)

        header_data = self.__headerdata(base64_token)

        post_data = "grant_type=client_credentials"
        try:
            req = requests.post(
                API_HOST + "/oauth2/token",
                data=post_data,
                headers=header_data,
                timeout=10,
            )

            if not req.ok:
                return False
        except TimeoutError:
            _LOGGER.warning("Timeout communication with Danfoss Ally API")
            return False
        except:
            _LOGGER.warning(
                "Unexpected error occured in communications with Danfoss Ally API!"
            )
            return False

        callData = req.json()

        if callData is False:
            return False

        expires_in = float(callData["expires_in"])
        self._refresh_at = datetime.datetime.now()
        self._refresh_at = self._refresh_at + datetime.timedelta(seconds=expires_in)
        self._refresh_at = self._refresh_at + datetime.timedelta(seconds=-30)
        self._token = callData["access_token"]
        return True

    def get_devices(self) -> dict:
        """Get list of all devices."""
        callData = self._call("/ally/devices")

        return callData

    def get_device(self, device_id: str) -> dict:
        """Get device details."""
        callData = self._call("/ally/devices/" + device_id)

        return callData

    def set_temperature(
        self, device_id: str, temp: int, code: str = "manual_mode_fast"
    ) -> bool:
        """Set temperature setpoint."""

        request_body = {"commands": [{"code": code, "value": temp}]}

        callData = self._call(
            "/ally/devices/" + device_id + "/commands", payload=request_body
        )

        _LOGGER.debug(
            "Set temperature for device %s: %s", device_id, json.dumps(request_body)
        )

        return callData["result"]

    def set_mode(self, device_id: str, mode: str) -> bool:
        """Set device operating mode."""
        commands = [{"code": "mode", "value": mode}]

        # Strangely, some modes require `last_click_time` to also be set to "YYYYmmddHHMM{{mode_in_last_click_time_format}}"
        if mode in _MODE_TO_LAST_CLICK_TIME_FORMAT_MAP:
            commands.append(
                {
                    "code": "last_click_time",
                    "value": f"{datetime.datetime.now():%Y%m%d%H%M}{_MODE_TO_LAST_CLICK_TIME_FORMAT_MAP[mode]}",
                }
            )

        request_body = {"commands": commands}

        callData = self._call(
            "/ally/devices/" + device_id + "/commands", payload=request_body
        )

        _LOGGER.debug(
            "Set mode for device %s: %s", device_id, json.dumps(request_body)
        )

        return callData["result"]

    def send_command(
        self, device_id: str, listofcommands: list[tuple[str, str]]
    ) -> bool:
        """Send commands."""

        commands = []
        for code, value in listofcommands:
            commands += [{"code": code, "value": value}]
        request_body = {"commands": commands}

        _LOGGER.debug("Sending command: %s", request_body)

        callData = self._call(
            "/ally/devices/" + device_id + "/commands", payload=request_body
        )

        return callData["result"]

    @property
    def token(self) -> str:
        """Return token."""
        return self._token
