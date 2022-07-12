"""For testing the module."""
from os import environ
from pprint import pprint

from pydanfossally import DanfossAlly

ally = DanfossAlly()
ally.initialize(environ["KEY"], environ["SECRET"])

if not ally.authorized:
    print("Error in authorization")

ally.getDeviceList()

pprint(vars(ally))