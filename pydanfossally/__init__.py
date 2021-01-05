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

    async def async_initialize(self, key, secret):
        """Authorize and initialize the connection."""
        #loop = asyncio.get_running_loop()
        self._apikey = key
        self._apisecret = secret

        #token = await loop.run_in_executor(None,
        #                                  self._api.getToken,
        #                                  key,
        #                                  secret)
        token = await self._api.async_getToken(key, secret)
        
        if token is False:
            self._authorized = False
            return False

        self._token = token
        self._authorized = True
        return True

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