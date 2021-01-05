import asyncio, concurrent.futures
import base64
import datetime
import json
import requests

API_HOST = "https://api.danfoss.com"


class DanfossAllyAPI():
    def __init__(self):
        """Init API."""
        self._key = ''
        self._secret = ''
        self._token = ''
        self._refresh_at = datetime.datetime.now()

    async def _async_call(self, path, headers_data, payload=None):
        """Do the actual API call async."""

        await self._refresh_token()
        try:
            if payload:
                req = requests.post(API_HOST + path, data=payload, headers=headers_data, timeout=10)
            else:
                req = requests.get(API_HOST + path, headers=headers_data, timeout=10)

            if not req.ok:
                return False
        except TimeoutError:
            print("Timeout communication with Danfoss Ally API")
            raise
            return False
        except:
            print("Unexpected error occured!")
            raise
            return False

        return req.json()

    def _call(self, path, headers_data, payload=None):
        """Redirect sync API call to async."""

        pool = concurrent.futures.ThreadPoolExecutor()
        result = pool.submit(asyncio.run, self._async_call(path, headers_data, payload)).result()

        return result

    async def _refresh_token(self):
        if self._refresh_at > datetime.datetime.now():
            print("We are good to go")
            return False

        await async_getToken()

    async def async_getToken(self, key=None, secret=None):
        """Get token."""

        if not key is None:
            self._key = key
        if not secret is None:
            self._secret = secret

        encStr = self._key + ':' + self._secret
        encBytes = encStr.encode('ascii')
        encoded = base64.b64encode(encBytes).decode('ascii')

        header_data = {}
        header_data['Content-Type'] = 'application/x-www-form-urlencoded'
        header_data['Authorization'] = 'Basic ' + encoded
        header_data['Accept'] = 'application/json'

        post_data = 'grant_type=client_credentials'

        req = requests.post(API_HOST + '/oauth2/token', data=post_data, headers=header_data, timeout=10)

        callData = req.json()

        if callData is False:
            return False
        else:
            expires_in = float(callData['expires_in'])
            self._refresh_at = datetime.datetime.now()
            self._refresh_at = self._refresh_at + datetime.timedelta(seconds=expires_in)
            self._refresh_at = self._refresh_at + datetime.timedelta(seconds=-30)
            self._token = callData['access_token']
            return True

    def get_devices(self):
        """Get list of all devices."""

        header_data = {}
        header_data['Accept'] = 'application/json'
        header_data['Authorization'] = 'Bearer ' + self._token

        callData = self._call('/ally/devices', header_data)

        return callData

    async def async_get_device(self, device_id):
        """Get device details."""

        header_data = {}
        header_data['Accept'] = 'application/json'
        header_data['Authorization'] = 'Bearer ' + self._token

        callData = await self._async_call(
            '/ally/devices/' + device_id, header_data
        )

        return callData
