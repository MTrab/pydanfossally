"""Small async read-only example for the Danfoss Ally client."""

from __future__ import annotations

import asyncio
from os import environ
from pprint import pprint

from pydanfossally import DanfossAlly


async def main() -> None:
    """Run a simple read-only session against the Danfoss Ally API."""
    async with DanfossAlly(
        refresh_device_concurrency=5,
        refresh_device_min_interval=0.35,
    ) as ally:
        authorized = await ally.initialize(environ["KEY"], environ["SECRET"])
        if not authorized:
            raise RuntimeError("Error in authorization")

        devices = await ally.get_devices()
        pprint(devices)


if __name__ == "__main__":
    asyncio.run(main())
