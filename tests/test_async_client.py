"""Tests for the async Danfoss Ally client."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from pydanfossally import DanfossAlly, parse_device_data
from pydanfossally.danfossallyapi import API_HOST, DanfossAllyAPI
from pydanfossally.exceptions import BadRequestError, RateLimitError


TOKEN_RESPONSE = {"access_token": "token-123", "expires_in": 3600}
DEVICE_PAYLOAD = {
    "id": "device-1",
    "name": " Living room ",
    "online": True,
    "update_time": 123456,
    "device_type": "Danfoss Ally Thermostat",
    "status": [
        {"code": "temp_set", "value": 215},
        {"code": "temp_current", "value": 203},
        {"code": "humidity_value", "value": 455},
        {"code": "battery_percentage", "value": 97},
        {"code": "window_state", "value": "open"},
        {"code": "switch_state", "value": "true"},
        {"code": "mode", "value": "manual"},
    ],
}

THERMOSTAT_WITH_MODE_SETPOINTS = {
    **DEVICE_PAYLOAD,
    "status": [
        {"code": "manual_mode_fast", "value": 215},
        {"code": "at_home_setting", "value": 200},
        {"code": "leaving_home_setting", "value": 170},
        {"code": "pause_setting", "value": 70},
        {"code": "holiday_setting", "value": 140},
        {"code": "temp_current", "value": 203},
        {"code": "humidity_value", "value": 455},
        {"code": "battery_percentage", "value": 97},
        {"code": "window_state", "value": "open"},
        {"code": "switch_state", "value": "true"},
        {"code": "mode", "value": "manual"},
    ],
}


class DanfossAllyAsyncTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the async-first client stack."""

    async def test_default_http_client_is_created_lazily(self) -> None:
        """The default HTTP client should not be created during __init__."""
        api = DanfossAllyAPI()

        self.assertIsNone(api._client)

        with patch("pydanfossally.danfossallyapi.httpx.AsyncClient") as async_client:
            mocked_client = unittest.mock.AsyncMock()
            mocked_client.post.return_value = httpx.Response(
                200,
                json=TOKEN_RESPONSE,
                request=httpx.Request("POST", f"{API_HOST}/oauth2/token"),
            )
            async_client.return_value = mocked_client

            result = await api.get_token("key", "secret")

        self.assertTrue(result)
        async_client.assert_called_once()
        mocked_client.post.assert_awaited_once()
        await api.aclose()

    async def _make_api(self, handler) -> DanfossAllyAPI:
        client = httpx.AsyncClient(
            base_url=API_HOST,
            transport=httpx.MockTransport(handler),
        )
        self.addAsyncCleanup(client.aclose)
        return DanfossAllyAPI(client=client)

    async def test_get_devices_populates_wrapper_cache(self) -> None:
        """The wrapper should fetch and parse devices through the async API."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                self.assertEqual(request.headers["Authorization"], "Bearer token-123")
                return httpx.Response(200, json={"result": [DEVICE_PAYLOAD], "t": 1})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))

        authorized = await ally.initialize("key", "secret")
        devices = await ally.get_devices()

        self.assertTrue(authorized)
        self.assertIn("device-1", devices)
        self.assertEqual(devices["device-1"]["name"], "Living room")
        self.assertEqual(devices["device-1"]["temperature"], 20.3)
        self.assertTrue(devices["device-1"]["window_open"])
        self.assertEqual(devices["device-1"]["last_response_time"], 1)

    async def test_get_device_maps_last_response_time(self) -> None:
        """Single-device reads should keep the API response timestamp."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices/device-1":
                return httpx.Response(200, json={"result": DEVICE_PAYLOAD, "t": 12})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        device = await ally.get_device("device-1")

        assert device is not None
        self.assertEqual(device["last_response_time"], 12)

    async def test_get_device_status_and_sub_devices(self) -> None:
        """Spec-defined read-only endpoints should be available."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices/device-1/status":
                return httpx.Response(
                    200,
                    json={"result": [{"code": "temp_set", "value": 215}], "t": 2},
                )
            if request.url.path == "/ally/devices/device-1/sub-devices":
                return httpx.Response(
                    200,
                    json={"result": [{"id": "child-1", "name": "Child"}], "t": 3},
                )
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        status = await ally.get_device_status("device-1")
        sub_devices = await ally.get_device_sub_devices("device-1")

        self.assertEqual(status[0]["code"], "temp_set")
        self.assertEqual(sub_devices[0]["id"], "child-1")

    async def test_refresh_device_uses_single_device_endpoint(self) -> None:
        """Per-device refresh should only use the single-device payload."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices/device-1":
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            **DEVICE_PAYLOAD,
                            "status": [
                                {"code": "temp_set", "value": 230},
                                {"code": "temp_current", "value": 205},
                                {"code": "mode", "value": "manual"},
                            ],
                        },
                        "t": 13,
                    },
                )
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        device = await ally.refresh_device("device-1")

        assert device is not None
        self.assertEqual(device["temp_set"], 23.0)
        self.assertEqual(device["temperature"], 20.5)
        self.assertEqual(device["last_response_time"], 13)

    async def test_refresh_devices_limits_parallelism_to_ten(self) -> None:
        """Cached device refreshes should use bounded parallelism."""
        ally = DanfossAlly(
            api=await self._make_api(lambda request: httpx.Response(200))
        )
        ally.devices = {f"device-{idx}": {"id": f"device-{idx}"} for idx in range(8)}

        active_calls = 0
        max_active_calls = 0

        async def fake_refresh_device(device_id: str) -> dict[str, object]:
            nonlocal active_calls, max_active_calls
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
            await asyncio.sleep(0)
            active_calls -= 1
            return {"id": device_id}

        ally.refresh_device = fake_refresh_device  # type: ignore[method-assign]

        await ally.refresh_devices()

        self.assertEqual(max_active_calls, 8)

    async def test_send_command_accepts_result_shape(self) -> None:
        """Command responses with a result field should be accepted."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices/device-1/commands":
                payload = json.loads(request.content.decode())
                self.assertEqual(
                    payload,
                    {"commands": [{"code": "temp_set", "value": 220}]},
                )
                return httpx.Response(201, json={"result": True, "t": 4})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        result = await ally.send_command("device-1", [("temp_set", 220)])

        self.assertTrue(result)

    async def test_send_command_accepts_timestamp_only_shape(self) -> None:
        """Command responses with only a timestamp should still be treated as success."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices/device-1/commands":
                return httpx.Response(201, json={"t": 5})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        self.assertTrue(await ally.set_temperature("device-1", 22.0, code="temp_set"))

    async def test_set_temperature_for_mode_sets_mode_before_mode_setpoint(
        self,
    ) -> None:
        """Mode-aware temperature writes should set the matching mode first."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return httpx.Response(
                    200,
                    json={"result": [THERMOSTAT_WITH_MODE_SETPOINTS], "t": 1},
                )
            if request.url.path == "/ally/devices/device-1/commands":
                payload = json.loads(request.content.decode())
                if payload == {"commands": [{"code": "mode", "value": "manual"}]}:
                    return httpx.Response(201, json={"result": True, "t": 6})
                if payload == {
                    "commands": [{"code": "manual_mode_fast", "value": 230}]
                }:
                    return httpx.Response(201, json={"result": True, "t": 7})
                raise AssertionError(f"Unexpected command payload: {payload}")
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()

        self.assertTrue(await ally.set_temperature_for_mode("device-1", 23.0, "manual"))

    async def test_set_manual_temperature_is_manual_mode_helper(self) -> None:
        """Manual temperature helper should reuse manual mode semantics."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return httpx.Response(
                    200,
                    json={"result": [THERMOSTAT_WITH_MODE_SETPOINTS], "t": 1},
                )
            if request.url.path == "/ally/devices/device-1/commands":
                payload = json.loads(request.content.decode())
                if payload == {"commands": [{"code": "mode", "value": "manual"}]}:
                    return httpx.Response(201, json={"result": True, "t": 8})
                if payload == {
                    "commands": [{"code": "manual_mode_fast", "value": 225}]
                }:
                    return httpx.Response(201, json={"result": True, "t": 9})
                raise AssertionError(f"Unexpected command payload: {payload}")
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()

        self.assertTrue(await ally.set_manual_temperature("device-1", 22.5))

    async def test_set_temperature_for_mode_sets_auto_mode_before_auto_setpoint(
        self,
    ) -> None:
        """Mode-aware temperature writes should switch back to schedule mode."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return httpx.Response(
                    200,
                    json={"result": [THERMOSTAT_WITH_MODE_SETPOINTS], "t": 1},
                )
            if request.url.path == "/ally/devices/device-1/commands":
                payload = json.loads(request.content.decode())
                if payload["commands"][0] == {"code": "mode", "value": "at_home"}:
                    return httpx.Response(201, json={"result": True, "t": 10})
                if payload == {"commands": [{"code": "at_home_setting", "value": 210}]}:
                    return httpx.Response(201, json={"result": True, "t": 11})
                raise AssertionError(f"Unexpected command payload: {payload}")
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()

        self.assertTrue(
            await ally.set_temperature_for_mode("device-1", 21.0, "at_home")
        )

    async def test_bad_request_maps_to_domain_exception(self) -> None:
        """HTTP 400 should map to the dedicated request exception."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices/device-1/commands":
                return httpx.Response(400, json={"title": "Bad Request"})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        with self.assertRaises(BadRequestError):
            await ally.send_command("device-1", [("temp_set", 220)])

    async def test_rate_limit_maps_to_domain_exception(self) -> None:
        """HTTP 429 should map to the dedicated throttling exception."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return httpx.Response(429, json={"title": "Too Many Requests"})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        with self.assertRaises(RateLimitError):
            await ally.get_devices()


class ParseDeviceDataTests(unittest.TestCase):
    """Validate the best-effort parsing layer separately from transport."""

    def test_parse_device_data_maps_known_fields(self) -> None:
        """Known status codes should be normalized into friendly fields."""
        parsed = parse_device_data(DEVICE_PAYLOAD)

        self.assertEqual(parsed["name"], "Living room")
        self.assertEqual(parsed["model"], "Danfoss Ally Thermostat")
        self.assertEqual(parsed["temp_set"], 21.5)
        self.assertEqual(parsed["temperature"], 20.3)
        self.assertEqual(parsed["humidity"], 45.5)
        self.assertEqual(parsed["battery"], 97)
        self.assertTrue(parsed["switch_state"])
        self.assertEqual(parsed["mode"], "manual")
        self.assertTrue(parsed["isThermostat"])

    def test_parse_device_data_accepts_last_response_time(self) -> None:
        """The parser should expose the API response timestamp when provided."""
        parsed = parse_device_data(DEVICE_PAYLOAD, last_response_time=1773499838298)

        self.assertEqual(parsed["last_response_time"], 1773499838298)
