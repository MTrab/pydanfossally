import asyncio

from .danfossallyapi import *

__version__ = '0.0.1'


class DanfossAlly:
    """Danfoss Ally API connector."""

    def __init__(self):
        self._apikey =  ''
        self._apisecret = ''
        self._token = ''
        self._authorized = False

        self._api = DanfossAllyAPI()

    async def initialize(self, key, secret):
        loop = asyncio.get_running_loop()

        token = await loop.run_in_executor(None,
                                          self._api.getToken,
                                          key,
                                          secret)
        
        if token is False:
            self._authorized = False
            return None

        self._authorized = True
        return True