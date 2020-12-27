import asyncio

API_HOST = "https://api.danfoss.com"


class DanfossAllyAPI():
    def __init__(self):
        """Init API."""

    def _call(self, path, headers_data, payload=None):
        """Do the actual API call."""
        import requests

        try:
            if payload:
                req = requests.post(API_HOST + path, data=payload, headers=headers_data, timeout=10)
            else:
                req = requests.get(API_HOST + path, headers=headers_data, timeout=10)

            if not req.ok:
                return False
        except:
            raise("Timeout connecting")
            return False

        return req.json()

    def getToken(self, key, secret):
        """Get token."""
        import json
        import base64

        encoded = base64.b64encode(key + ':' + secret)

        header_data = {}
        header_data['Content-Type'] = 'application/x-www-form-urlencoded'
        header_data['Authorization'] = 'Basic ' + encoded
        header_data['Accept'] = 'application/json'

        post_data = 'grant_type=client_credentials'

        callData = self._call('/oauth2/token', header_data, post_data)