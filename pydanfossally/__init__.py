import asyncio

from .danfossallyapi import *

__version__ = '0.0.7'


class DanfossAlly:
    """Danfoss Ally API connector."""

    def __init__(self):
        """Init the API connector variables."""
        self._apikey =  ''
        self._apisecret = ''
        self._token = ''
        self._authorized = False
        self.devices = {}

        self._api = DanfossAllyAPI()

    async def _refresh_token(self):
        if self._refresh_at >= datetime.datetime.now():
            return False

        expires_in = float(token['expires_in'])
        self._refresh_at = datetime.datetime.now()
        self._refresh_at = self._refresh_at + datetime.timedelta(seconds=expires_in)

    async def async_initialize(self, key, secret):
        """Authorize and initialize the connection."""
        self._apikey = key
        self._apisecret = secret

        token = await self._api.async_getToken(key, secret)
        
        if token is False:
            self._authorized = False
            return False

        self._token = token['token']
        self._authorized = True
        return self._authorized

    def getDeviceList(self):
        """Get device list."""
        #loop = asyncio.get_running_loop()
        #devices = loop.run_in_executor(None, self._api.get_devices, self._token)
        devices = self._api.get_devices(self._token)
        for device in devices['result']:
            self.devices[device['id']] = {}
            self.devices[device['id']]['isThermostat'] = False
            self.devices[device['id']]['name'] = device['name'].strip()
            self.devices[device['id']]['online'] = device['online']
            self.devices[device['id']]['update'] = device['update_time']
            if 'model' in device:
                self.devices[device['id']]['model'] = device['model']
            for status in device['status']:
                if status['code'] == 'temp_set':
                    setpoint = float(status['value'])
                    setpoint = setpoint/10
                    self.devices[device['id']]['setpoint'] = setpoint
                    self.devices[device['id']]['isThermostat'] = True
                elif status['code'] == 'mode':
                    self.devices[device['id']]['mode'] = status['value']


    async def getDevice(self, device_id):
        """Get device data."""
        device = await self._api.get_device(self._token, device_id)

    @property
    def authorized(self):
        """Return authorized status."""
        return self._authorized