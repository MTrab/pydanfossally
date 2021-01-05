import asyncio
import json

API_HOST = "https://api.danfoss.com"


class DanfossAllyAPI():
    def __init__(self):
        """Init API."""

    async def _call(self, path, headers_data, payload=None):
        """Do the actual API call."""
        import requests

        #try:
        if payload:
            req = requests.post(API_HOST + path, data=payload, headers=headers_data, timeout=10)
        else:
            req = requests.get(API_HOST + path, headers=headers_data, timeout=10)

        if not req.ok:
            return False
        #except TimeoutError:
        #    print("Timeout communication with Danfoss Ally API")
        #    raise
        #    return False
        #except:
        #    print("Unexpected error occured!")
        #    raise
        #    return False

        return req.json()

    async def getToken(self, key, secret):
        """Get token."""
        import base64

        encStr = key + ':' + secret
        encBytes = encStr.encode('ascii')
        encoded = base64.b64encode(encBytes).decode('ascii')

        header_data = {}
        header_data['Content-Type'] = 'application/x-www-form-urlencoded'
        header_data['Authorization'] = 'Basic ' + encoded
        header_data['Accept'] = 'application/json'

        post_data = 'grant_type=client_credentials'

        callData = await self._call('/oauth2/token', header_data, post_data)

        if callData is False:
            return False

        return callData['access_token']

    async def get_devices(self, token):
        """Get list of all devices."""

        header_data = {}
        header_data['Accept'] = 'application/json'
        header_data['Authorization'] = 'Bearer ' + token

        callData = await self._call('/ally/devices', header_data)

        return callData

    async def get_device(self, token, device_id):
        """Get device details."""

        header_data = {}
        header_data['Accept'] = 'application/json'
        header_data['Authorization'] = 'Bearer ' + token

        callData = await self._call('/ally/devices/' + device_id, header_data)

        return callData
