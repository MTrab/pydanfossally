"""Doing the communications in this file."""
from __future__ import annotations

import base64
import datetime
import json
import logging

import requests

API_HOST = "https://api.danfoss.com"

_LOGGER = logging.getLogger(__name__)


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
                req = requests.post(
                    API_HOST + path, json=payload, headers=headers_data, timeout=10
                )
            else:
                req = requests.get(API_HOST + path, headers=headers_data, timeout=10)

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

        json = req.json()
        print("JSON: ", json)
        return json

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
        request_body = {"commands": [{"code": "mode", "value": mode}]}

        callData = self._call(
            "/ally/devices/" + device_id + "/commands", payload=request_body
        )

        return callData["result"]

    @property
    def token(self) -> str:
        """Return token."""
        return self._token
