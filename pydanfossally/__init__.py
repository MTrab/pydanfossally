import asyncio

from .danfossallyapi import *

__version__ = '0.0.3'


class DanfossAlly:
    """Danfoss Ally API connector."""

    def __init__(self):
        """Init the API connector variables."""
        self._apikey =  ''
        self._apisecret = ''
        self._token = ''
        self._authorized = False
        self._devices = {}

        self._api = DanfossAllyAPI()

    async def initialize(self, key, secret):
        """Authorize and initialize the connection."""
        loop = asyncio.get_running_loop()

        token = await loop.run_in_executor(None,
                                          self._api.getToken,
                                          key,
                                          secret)
        
        if token is False:
            self._authorized = False
            return False

        self._token = token
        self._authorized = True
        return True

    async def getDeviceList(self):
        """Get device list."""
        devices = self._api.get_devices(self._token)
        for device in devices['result']:
            self._devices[device['id']] = {}
            self._devices[device['id']]['isThermostat'] = False
            self._devices[device['id']]['name'] = device['name']
            self._devices[device['id']]['online'] = device['online']
            self._devices[device['id']]['update'] = device['update_time']
            if 'model' in device:
                self._devices[device['id']]['model'] = device['model']
            for status in device['status']:
                if status['code'] == 'temp_set':
                    setpoint = float(status['value'])
                    setpoint = setpoint/10
                    self._devices[device['id']]['setpoint'] = setpoint
                    self._devices[device['id']]['isThermostat'] = True
                elif status['code'] == 'mode':
                    self._devices[device['id']]['mode'] = status['value']


    async def getDevice(self, device_id):
        """Get device data."""
        device = self._api.get_device(self._token, device_id)