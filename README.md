<a href="https://www.buymeacoffee.com/mtrab" target="_blank"><img src="https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png" alt="Buy Me A Coffee" style="height: 41px !important;width: 174px !important;box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;-webkit-box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;"></a>

# Danfoss Ally API

Async-first Python client for the Danfoss Ally OpenAPI.

## Installation

```bash
pip install pydanfossally
```

## Local development

For local development with Poetry and VS Code debugging, this repository is configured to use
an in-project virtual environment at `.venv`.

```bash
poetry install
poetry run python example.py
```

If you already created a Poetry environment before this setting was added, recreate it once so
Poetry installs dependencies into `.venv` for this repository.

## Async usage

### Long-lived client

This is the recommended pattern for smart-home systems and other integrations that keep one
client alive and reuse it across many calls.

```python
from pydanfossally import DanfossAlly

ally = DanfossAlly(
    timeout=30,
    refresh_device_concurrency=5,
    refresh_device_min_interval=0.10,
    device_discovery_interval=600,
    degraded_refresh_cooldown=600,
    hot_refresh_timeout=300,
    user_agent_prefix="HomeAssistant-DanfossAlly/2026.3.0",
)

authorized = await ally.initialize(key, secret)
if not authorized:
    raise RuntimeError("Authorization failed")

devices = await ally.get_devices()
print(devices)

await ally.aclose()
```

### Context-managed client

This is a good fit for small scripts, one-off tools, and examples where you want automatic
resource cleanup.

```python
import asyncio
import os

from pydanfossally import DanfossAlly


async def main() -> None:
    async with DanfossAlly(
        timeout=30,
        refresh_device_concurrency=5,
        refresh_device_min_interval=0.10,
        device_discovery_interval=600,
        degraded_refresh_cooldown=600,
        hot_refresh_timeout=300,
        user_agent_prefix="HomeAssistant-DanfossAlly/2026.3.0",
    ) as ally:
        authorized = await ally.initialize(os.environ["KEY"], os.environ["SECRET"])
        if not authorized:
            raise RuntimeError("Authorization failed")

        devices = await ally.get_devices()
        print(devices)


asyncio.run(main())
```

## Supported OpenAPI endpoints

- `POST /oauth2/token`
- `GET /ally/devices`
- `GET /ally/devices/{device_id}`
- `GET /ally/devices/{device_id}/sub-devices`
- `GET /ally/devices/{device_id}/status`
- `POST /ally/devices/{device_id}/commands`

## Notes about the data model

The transport layer follows the OpenAPI file in [`docs/openapi-spec`](docs/openapi-spec).

The parsed `devices` mapping is a best-effort convenience model built from observed status
codes. The OpenAPI file documents generic `{code, value}` pairs, but it does not define all
device-specific status or command codes. That means:

- request/response transport compatibility is covered by the library
- friendly parsed fields are based on current observed API behavior
- some status fields may vary between device types

## Known gaps

- The OpenAPI file does not fully document which command `code` values are supported for all
  device types.
- The `POST /commands` response shape is inconsistent between schema and examples; this client
  accepts both `{"result": true, "t": ...}` and `{"t": ...}`.
- Live verification should be performed against read-only endpoints before enabling write flows
  in production integrations.

## Refresh behavior

Each `refresh_devices()` call always starts with a bulk read from `GET /ally/devices`.

After successful write operations (`set_mode(...)` and `send_command(...)`), the target device is
tracked as pending hot refresh. While pending, `refresh_devices()` also calls
`GET /ally/devices/{device_id}/status` for that device.

Hot refresh for a pending device stops when either of these conditions is met:

- the latest bulk snapshot differs from the baseline state captured when the write succeeded
- the hot refresh timeout is reached (default: 300 seconds / 5 minutes)

The refresh tuning knobs are configurable through `DanfossAlly(...)`:

- `refresh_device_concurrency` controls how many status hot refresh calls may run at once
- `refresh_device_min_interval` controls the minimum delay in seconds between starting two
  status hot refresh calls
- `hot_refresh_timeout` controls how long a device stays in pending hot refresh mode

## User-Agent

By default, the client sends a `User-Agent` header in the form `pydanfossally/<version>`.
Integrations can prepend their own identifier through `user_agent_prefix`, resulting in a final
header such as `HomeAssistant-DanfossAlly/2026.3.0 pydanfossally/<version>`.

## Local verification

The repository includes `example.py` as a small async read-only example that uses credentials
from the environment.
