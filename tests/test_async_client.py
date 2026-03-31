"""Tests for the async Danfoss Ally client."""

from __future__ import annotations

import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from yarl import URL

from pydanfossally import DanfossAlly, parse_device_data
from pydanfossally.danfossallyapi import API_HOST, DanfossAllyAPI
from pydanfossally.exceptions import (
    BadRequestError,
    ForbiddenError,
    InternalServerError,
    RateLimitError,
)


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

GATEWAY_PAYLOAD = {
    "id": "gateway-1",
    "name": " Gateway ",
    "online": True,
    "update_time": 123457,
    "device_type": "Danfoss Ally Gateway",
    "status": [
        {"code": "mode", "value": "connected"},
    ],
}

BOILER_RELAY_PAYLOAD = {
    "id": "relay-1",
    "name": " Boiler relay ",
    "online": True,
    "update_time": 123459,
    "device_type": "Danfoss Ally\u2122 Boiler Relay",
    "status": [
        {"code": "switch_state", "value": "true"},
    ],
}

ROOM_SENSOR_PAYLOAD = {
    "id": "sensor-1",
    "name": " Bedroom sensor ",
    "online": True,
    "update_time": 123458,
    "device_type": "Danfoss Ally Room Sensor",
    "status": [
        {"code": "local_temperature", "value": 2134},
        {"code": "humidity_value", "value": 501},
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


def clone_device_payload(
    payload: dict[str, object],
    *,
    device_id: str | None = None,
    name: str | None = None,
) -> dict[str, object]:
    """Clone a payload and optionally override core identifiers."""
    clone = copy.deepcopy(payload)
    if device_id is not None:
        clone["id"] = device_id
    if name is not None:
        clone["name"] = name
    return clone


class MockRequest:
    """Small request object for transport-free client tests."""

    def __init__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        content: bytes = b"",
    ) -> None:
        self.method = method
        self.url = URL(url)
        self.headers = headers
        self.content = content


class MockResponse:
    """Async context manager that mimics aiohttp responses."""

    def __init__(
        self,
        status: int,
        *,
        json_data: object | None = None,
        body: bytes | None = None,
    ) -> None:
        self.status = status
        if body is not None:
            self._body = body
        elif json_data is not None:
            self._body = json.dumps(json_data).encode()
        else:
            self._body = b""

    async def __aenter__(self) -> MockResponse:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def read(self) -> bytes:
        return self._body


class MockClientSession:
    """Tiny stand-in for the async HTTP client."""

    def __init__(
        self,
        handler,
        *,
        base_url: str = API_HOST,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._handler = handler
        self._base_url = base_url.rstrip("/")
        self._headers = headers or {}
        self.closed = False

    def request(self, method: str, path: str, **kwargs: object) -> MockResponse:
        payload = kwargs.get("json")
        headers = kwargs.get("headers")
        content = b"" if payload is None else json.dumps(payload).encode()
        merged_headers = {**self._headers, **dict(headers or {})}
        return self._handler(
            MockRequest(method, f"{self._base_url}{path}", merged_headers, content)
        )

    def post(self, path: str, **kwargs: object) -> MockResponse:
        data = kwargs.get("data")
        headers = kwargs.get("headers")
        if isinstance(data, str):
            content = data.encode()
        elif isinstance(data, bytes):
            content = data
        else:
            content = b""
        merged_headers = {**self._headers, **dict(headers or {})}
        return self._handler(
            MockRequest("POST", f"{self._base_url}{path}", merged_headers, content)
        )

    async def close(self) -> None:
        self.closed = True


class DanfossAllyAsyncTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the async-first client stack."""

    async def test_default_http_client_is_created_lazily(self) -> None:
        """The default HTTP client should not be created during __init__."""
        api = DanfossAllyAPI()

        self.assertIsNone(api._client)

        with patch("pydanfossally.danfossallyapi.aiohttp.ClientSession") as session_cls:
            response = MockResponse(200, json_data=TOKEN_RESPONSE)
            mocked_client = unittest.mock.Mock()
            mocked_client.post.return_value = response
            mocked_client.close = unittest.mock.AsyncMock()
            session_cls.return_value = mocked_client

            result = await api.get_token("key", "secret")

        self.assertTrue(result)
        session_cls.assert_called_once()
        mocked_client.post.assert_called_once()
        await api.aclose()

    def test_default_user_agent_starts_with_safe_fallback(self) -> None:
        """The constructor should not resolve package metadata synchronously."""
        api = DanfossAllyAPI()

        self.assertEqual(api._user_agent, "pydanfossally/unknown")

    async def test_user_agent_is_resolved_during_async_initialization(self) -> None:
        """The user agent version should be resolved lazily in async code."""
        with patch(
            "pydanfossally.danfossallyapi.metadata.version",
            return_value="1.2.3",
        ):
            api = DanfossAllyAPI(
                user_agent_prefix="HomeAssistant-DanfossAlly/2026.3.0",
            )
            await api._ensure_user_agent()

            self.assertEqual(
                api._user_agent,
                "HomeAssistant-DanfossAlly/2026.3.0 pydanfossally/1.2.3",
            )

    async def test_requests_send_prefixed_user_agent_header(self) -> None:
        """Outgoing request headers should include the composed user agent."""
        with patch(
            "pydanfossally.danfossallyapi.metadata.version",
            return_value="1.2.3",
        ):
            api = DanfossAllyAPI(
                user_agent_prefix="HomeAssistant-DanfossAlly/2026.3.0",
            )
            await api._ensure_user_agent()

        self.assertEqual(
            api._auth_headers()["User-Agent"],
            "HomeAssistant-DanfossAlly/2026.3.0 pydanfossally/1.2.3",
        )
        self.assertEqual(
            api._basic_auth_headers("token")["User-Agent"],
            "HomeAssistant-DanfossAlly/2026.3.0 pydanfossally/1.2.3",
        )

    async def test_wrapper_passes_user_agent_prefix_to_default_api(self) -> None:
        """The high-level wrapper should pass user agent prefixes to its API client."""
        with patch(
            "pydanfossally.danfossallyapi.metadata.version",
            return_value="1.2.3",
        ):
            ally = DanfossAlly(user_agent_prefix="HomeAssistant-DanfossAlly/2026.3.0")
            await ally._api._ensure_user_agent()

        self.assertEqual(
            ally._api._user_agent,
            "HomeAssistant-DanfossAlly/2026.3.0 pydanfossally/1.2.3",
        )

    async def _make_api(
        self,
        handler,
    ) -> DanfossAllyAPI:
        client = MockClientSession(handler, base_url=API_HOST)
        self.addAsyncCleanup(client.close)
        return DanfossAllyAPI(client=client)

    async def test_get_devices_populates_wrapper_cache(self) -> None:
        """The wrapper should fetch and parse devices through the async API."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                self.assertEqual(request.headers["Authorization"], "Bearer token-123")
                return MockResponse(200, json_data={"result": [DEVICE_PAYLOAD], "t": 1})
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

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices/device-1":
                return MockResponse(200, json_data={"result": DEVICE_PAYLOAD, "t": 12})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        device = await ally.get_device("device-1")

        assert device is not None
        self.assertEqual(device["last_response_time"], 12)

    async def test_get_devices_keeps_newer_realtime_cache_than_bulk(self) -> None:
        """Bulk refresh should not overwrite a device with older than cached realtime data."""
        bulk_calls = 0

        def handler(request: MockRequest) -> MockResponse:
            nonlocal bulk_calls
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                bulk_calls += 1
                if bulk_calls == 1:
                    return MockResponse(
                        200, json_data={"result": [DEVICE_PAYLOAD], "t": 10}
                    )
                return MockResponse(
                    200,
                    json_data={
                        "result": [
                            {
                                **DEVICE_PAYLOAD,
                                "status": [
                                    {"code": "temp_set", "value": 180},
                                    {"code": "temp_current", "value": 181},
                                ],
                            }
                        ],
                        "t": 11,
                    },
                )
            if request.url.path == "/ally/devices/device-1":
                return MockResponse(
                    200,
                    json_data={
                        "result": {
                            **DEVICE_PAYLOAD,
                            "status": [
                                {"code": "temp_set", "value": 230},
                                {"code": "temp_current", "value": 205},
                            ],
                        },
                        "t": 20,
                    },
                )
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()
        await ally.get_device("device-1")

        devices = await ally.get_devices()

        self.assertEqual(devices["device-1"]["temp_set"], 23.0)
        self.assertEqual(devices["device-1"]["temperature"], 20.5)
        self.assertEqual(devices["device-1"]["last_response_time"], 20)
        self.assertEqual(
            ally.get_diagnostics()["skipped_refreshes"]["stale_bulk_device"],
            1,
        )

    async def test_get_devices_accepts_newer_bulk_than_cached_realtime(self) -> None:
        """Bulk refresh should overwrite cached devices when the bulk timestamp is newer."""
        bulk_calls = 0

        def handler(request: MockRequest) -> MockResponse:
            nonlocal bulk_calls
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                bulk_calls += 1
                if bulk_calls == 1:
                    return MockResponse(
                        200, json_data={"result": [DEVICE_PAYLOAD], "t": 10}
                    )
                return MockResponse(
                    200,
                    json_data={
                        "result": [
                            {
                                **DEVICE_PAYLOAD,
                                "status": [
                                    {"code": "temp_set", "value": 180},
                                    {"code": "temp_current", "value": 181},
                                ],
                            }
                        ],
                        "t": 30,
                    },
                )
            if request.url.path == "/ally/devices/device-1":
                return MockResponse(
                    200,
                    json_data={
                        "result": {
                            **DEVICE_PAYLOAD,
                            "status": [
                                {"code": "temp_set", "value": 230},
                                {"code": "temp_current", "value": 205},
                            ],
                        },
                        "t": 20,
                    },
                )
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()
        await ally.get_device("device-1")

        devices = await ally.get_devices()

        self.assertEqual(devices["device-1"]["temp_set"], 18.0)
        self.assertEqual(devices["device-1"]["temperature"], 18.1)
        self.assertEqual(devices["device-1"]["last_response_time"], 30)

    async def test_get_device_status_and_sub_devices(self) -> None:
        """Spec-defined read-only endpoints should be available."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices/device-1/status":
                return MockResponse(
                    200,
                    json_data={"result": [{"code": "temp_set", "value": 215}], "t": 2},
                )
            if request.url.path == "/ally/devices/device-1/sub-devices":
                return MockResponse(
                    200,
                    json_data={"result": [{"id": "child-1", "name": "Child"}], "t": 3},
                )
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        status = await ally.get_device_status("device-1")
        sub_devices = await ally.get_device_sub_devices("device-1")

        self.assertEqual(status[0]["code"], "temp_set")
        self.assertEqual(sub_devices[0]["id"], "child-1")

    async def test_api_diagnostics_count_requests_per_normalized_endpoint(self) -> None:
        """API diagnostics should count requests by stable endpoint names."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(200, json_data={"result": [DEVICE_PAYLOAD], "t": 1})
            if request.url.path == "/ally/devices/device-1/status":
                return MockResponse(
                    200,
                    json_data={"result": [{"code": "temp_set", "value": 215}], "t": 2},
                )
            if request.url.path == "/ally/devices/device-1/commands":
                return MockResponse(201, json_data={"result": True, "t": 3})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()
        await ally.get_device_status("device-1")
        await ally.send_command("device-1", [("temp_set", 220)])

        diagnostics = ally.get_diagnostics()

        self.assertEqual(diagnostics["request_counts"]["/oauth2/token"], 1)
        self.assertEqual(diagnostics["request_counts"]["/ally/devices"], 1)
        self.assertEqual(
            diagnostics["request_counts"]["/ally/devices/{device_id}/status"], 1
        )
        self.assertEqual(
            diagnostics["request_counts"]["/ally/devices/{device_id}/commands"], 1
        )

    async def test_bulk_diagnostics_log_one_key_per_line(self) -> None:
        """Bulk diagnostics should log a readable one-key-per-line summary."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(200, json_data={"result": [DEVICE_PAYLOAD], "t": 1})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        with self.assertLogs("pydanfossally", level="DEBUG") as captured_logs:
            await ally.get_devices()

        log_output = "\n".join(captured_logs.output)
        self.assertIn(
            "Diagnostics after bulk device snapshot (last 60m)",
            log_output,
        )
        self.assertIn("requests./ally/devices=1", log_output)
        self.assertIn("unsupported_status_device_count=0", log_output)
        self.assertIn("unsupported_status_devices=none", log_output)

    async def test_refresh_device_uses_status_endpoint_when_supported(self) -> None:
        """High-priority devices should prefer the status endpoint."""
        status_calls = 0
        device_calls = 0

        def handler(request: MockRequest) -> MockResponse:
            nonlocal status_calls, device_calls
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(200, json_data={"result": [DEVICE_PAYLOAD], "t": 1})
            if request.url.path == "/ally/devices/device-1/status":
                status_calls += 1
                return MockResponse(
                    200,
                    json_data={
                        "result": [
                            {"code": "temp_set", "value": 230},
                            {"code": "temp_current", "value": 205},
                            {"code": "mode", "value": "manual"},
                        ],
                        "t": 13,
                    },
                )
            if request.url.path == "/ally/devices/device-1":
                device_calls += 1
                return MockResponse(200, json_data={"result": DEVICE_PAYLOAD, "t": 12})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()

        device = await ally.refresh_device("device-1")

        assert device is not None
        self.assertEqual(device["temp_set"], 23.0)
        self.assertEqual(device["temperature"], 20.5)
        self.assertEqual(device["last_response_time"], 13)
        self.assertEqual(device["name"], "Living room")
        self.assertEqual(device["model"], "Danfoss Ally Thermostat")
        self.assertEqual(status_calls, 1)
        self.assertEqual(device_calls, 0)

    async def test_refresh_device_status_merges_cached_metadata(self) -> None:
        """Status refresh should preserve non-status metadata from the cache."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(200, json_data={"result": [DEVICE_PAYLOAD], "t": 1})
            if request.url.path == "/ally/devices/device-1/status":
                return MockResponse(
                    200,
                    json_data={
                        "result": [
                            {"code": "temp_set", "value": 225},
                            {"code": "temp_current", "value": 198},
                            {"code": "mode", "value": "manual"},
                        ],
                        "t": 14,
                    },
                )
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()

        device = await ally.refresh_device_status("device-1")

        assert device is not None
        self.assertEqual(device["name"], "Living room")
        self.assertEqual(device["model"], "Danfoss Ally Thermostat")
        self.assertEqual(device["temp_set"], 22.5)
        self.assertEqual(device["temperature"], 19.8)
        self.assertEqual(device["last_response_time"], 14)
        self.assertEqual(
            ally._device_metadata["device-1"]["name"],  # type: ignore[attr-defined]
            " Living room ",
        )
        self.assertEqual(
            ally._device_metadata["device-1"]["status"][0]["value"],  # type: ignore[attr-defined]
            225,
        )

    async def test_refresh_device_falls_back_after_unsupported_status_error(
        self,
    ) -> None:
        """Stable status-endpoint failures should switch the device to full refreshes."""
        status_calls = 0
        device_calls = 0

        def handler(request: MockRequest) -> MockResponse:
            nonlocal status_calls, device_calls
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(
                    200, json_data={"result": [ROOM_SENSOR_PAYLOAD], "t": 1}
                )
            if request.url.path == "/ally/devices/sensor-1/status":
                status_calls += 1
                return MockResponse(500, json_data={"title": "Internal Server Error"})
            if request.url.path == "/ally/devices/sensor-1":
                device_calls += 1
                return MockResponse(
                    200,
                    json_data={"result": ROOM_SENSOR_PAYLOAD, "t": 15},
                )
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()

        first_refresh = await ally.refresh_device("sensor-1")
        second_refresh = await ally.refresh_device("sensor-1")

        assert first_refresh is not None
        assert second_refresh is not None
        self.assertEqual(status_calls, 1)
        self.assertEqual(device_calls, 2)
        self.assertIn(
            "sensor-1",
            ally._status_refresh_unsupported,  # type: ignore[attr-defined]
        )

    async def test_refresh_devices_limits_parallelism_to_five(self) -> None:
        """Cached device refreshes should use bounded parallelism."""
        ally = DanfossAlly(
            api=await self._make_api(lambda request: MockResponse(200)),
            refresh_device_concurrency=5,
            refresh_device_min_interval=0,
        )
        for idx in range(8):
            ally._store_device(  # type: ignore[attr-defined]
                clone_device_payload(
                    DEVICE_PAYLOAD,
                    device_id=f"device-{idx}",
                    name=f" Thermostat {idx} ",
                )
            )

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

        self.assertEqual(max_active_calls, 5)

    async def test_refresh_devices_rate_limits_device_reads(self) -> None:
        """Cached device refreshes should be paced to avoid request bursts."""
        ally = DanfossAlly(
            api=await self._make_api(lambda request: MockResponse(200)),
            refresh_device_min_interval=0.02,
        )
        for idx in range(3):
            ally._store_device(  # type: ignore[attr-defined]
                clone_device_payload(
                    DEVICE_PAYLOAD,
                    device_id=f"device-{idx}",
                    name=f" Thermostat {idx} ",
                )
            )

        started_at: list[float] = []

        async def fake_refresh_device(device_id: str) -> dict[str, object]:
            started_at.append(asyncio.get_running_loop().time())
            return {"id": device_id}

        ally.refresh_device = fake_refresh_device  # type: ignore[method-assign]

        await ally.refresh_devices()

        self.assertEqual(len(started_at), 3)
        self.assertGreaterEqual(started_at[1] - started_at[0], 0.015)
        self.assertGreaterEqual(started_at[2] - started_at[1], 0.015)

    async def test_refresh_devices_falls_back_to_bulk_without_cache(self) -> None:
        """Realtime refresh should load a bulk snapshot when discovery data is missing."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(200, json_data={"result": [DEVICE_PAYLOAD], "t": 1})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        devices = await ally.refresh_devices()

        self.assertIn("device-1", devices)

    async def test_refresh_devices_skips_bulk_discovery_before_interval(self) -> None:
        """Known devices should use direct refresh until the discovery interval elapses."""
        bulk_calls = 0
        status_calls = 0

        def handler(request: MockRequest) -> MockResponse:
            nonlocal bulk_calls, status_calls
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                bulk_calls += 1
                return MockResponse(200, json_data={"result": [DEVICE_PAYLOAD], "t": 1})
            if request.url.path == "/ally/devices/device-1/status":
                status_calls += 1
                return MockResponse(
                    200,
                    json_data={
                        "result": [
                            {"code": "temp_set", "value": 230},
                            {"code": "temp_current", "value": 205},
                        ],
                        "t": 2,
                    },
                )
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(
            api=await self._make_api(handler),
            refresh_device_min_interval=0,
            device_discovery_interval=3600,
        )
        await ally.initialize("key", "secret")
        await ally.get_devices()

        await ally.refresh_devices()

        self.assertEqual(bulk_calls, 1)
        self.assertEqual(status_calls, 1)

    async def test_refresh_devices_renews_bulk_discovery_after_interval(self) -> None:
        """Hourly discovery should pick up new devices before direct refresh."""
        bulk_calls = 0
        status_calls: list[str] = []
        second_device = clone_device_payload(
            DEVICE_PAYLOAD,
            device_id="device-2",
            name=" Bedroom ",
        )

        def handler(request: MockRequest) -> MockResponse:
            nonlocal bulk_calls
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                bulk_calls += 1
                if bulk_calls == 1:
                    return MockResponse(
                        200, json_data={"result": [DEVICE_PAYLOAD], "t": 1}
                    )
                return MockResponse(
                    200,
                    json_data={"result": [DEVICE_PAYLOAD, second_device], "t": 2},
                )
            if request.url.path.endswith("/status"):
                device_id = request.url.path.split("/")[-2]
                status_calls.append(device_id)
                payload = DEVICE_PAYLOAD if device_id == "device-1" else second_device
                return MockResponse(
                    200, json_data={"result": payload["status"], "t": 3}
                )
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(
            api=await self._make_api(handler),
            refresh_device_min_interval=0,
            device_discovery_interval=3600,
        )
        await ally.initialize("key", "secret")
        await ally.get_devices()
        ally._next_device_discovery_at = asyncio.get_running_loop().time() - 1

        devices = await ally.refresh_devices()

        self.assertEqual(bulk_calls, 2)
        self.assertCountEqual(status_calls, ["device-1", "device-2"])
        self.assertIn("device-2", devices)

    async def test_refresh_devices_leaves_low_priority_devices_on_bulk_only(
        self,
    ) -> None:
        """Gateways and similar devices should not use per-device refreshes."""
        status_calls: list[str] = []

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(
                    200,
                    json_data={"result": [DEVICE_PAYLOAD, GATEWAY_PAYLOAD], "t": 1},
                )
            if request.url.path == "/ally/devices/device-1/status":
                status_calls.append("device-1")
                return MockResponse(
                    200,
                    json_data={"result": DEVICE_PAYLOAD["status"], "t": 2},
                )
            if request.url.path.startswith("/ally/devices/gateway-1"):
                raise AssertionError("Gateway should remain bulk-only")
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(
            api=await self._make_api(handler),
            refresh_device_min_interval=0,
            device_discovery_interval=3600,
        )
        await ally.initialize("key", "secret")
        await ally.get_devices()

        await ally.refresh_devices()

        self.assertEqual(status_calls, ["device-1"])

    def test_known_low_priority_device_types_are_bulk_only(self) -> None:
        """Known infrastructure device types should stay out of high-priority refreshes."""
        ally = DanfossAlly()
        ally._store_device(BOILER_RELAY_PAYLOAD)  # type: ignore[attr-defined]
        ally._store_device(GATEWAY_PAYLOAD)  # type: ignore[attr-defined]
        ally._store_device(DEVICE_PAYLOAD)  # type: ignore[attr-defined]

        self.assertFalse(ally._is_high_priority_device("relay-1"))  # type: ignore[attr-defined]
        self.assertFalse(ally._is_high_priority_device("gateway-1"))  # type: ignore[attr-defined]
        self.assertTrue(ally._is_high_priority_device("device-1"))  # type: ignore[attr-defined]

    def test_constructor_rejects_invalid_refresh_tuning(self) -> None:
        """Refresh tuning should reject invalid values early."""
        with self.assertRaises(ValueError):
            DanfossAlly(refresh_device_concurrency=0)

        with self.assertRaises(ValueError):
            DanfossAlly(refresh_device_min_interval=-0.1)

        with self.assertRaises(ValueError):
            DanfossAlly(device_discovery_interval=-0.1)

        with self.assertRaises(ValueError):
            DanfossAlly(degraded_refresh_cooldown=-0.1)

    async def test_refresh_devices_enters_degraded_mode_on_rate_limit(self) -> None:
        """429 during per-device refresh should trigger bulk-only cooldown and recovery."""
        ally = DanfossAlly(
            api=await self._make_api(lambda request: MockResponse(200)),
            refresh_device_concurrency=1,
            refresh_device_min_interval=0,
            degraded_refresh_cooldown=30,
        )
        ally._store_device(clone_device_payload(DEVICE_PAYLOAD, device_id="device-1"))  # type: ignore[attr-defined]
        ally._store_device(clone_device_payload(DEVICE_PAYLOAD, device_id="device-2"))  # type: ignore[attr-defined]

        refreshed_ids: list[str] = []
        attempts = 0

        async def fake_refresh_device(device_id: str) -> dict[str, object]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RateLimitError()
            refreshed_ids.append(device_id)
            return {"id": device_id}

        ally.refresh_device = fake_refresh_device  # type: ignore[method-assign]

        await ally.refresh_devices()
        self.assertGreater(
            ally._degraded_refresh_until,  # type: ignore[attr-defined]
            asyncio.get_running_loop().time(),
        )

        await ally.refresh_devices()
        self.assertEqual(refreshed_ids, [])

        ally._degraded_refresh_until = asyncio.get_running_loop().time() - 1  # type: ignore[attr-defined]
        await ally.refresh_devices()
        self.assertEqual(refreshed_ids, ["device-1"])

        await ally.refresh_devices()
        self.assertCountEqual(refreshed_ids, ["device-1", "device-1", "device-2"])

        diagnostics = ally.get_diagnostics()
        self.assertEqual(diagnostics["degraded_mode_entries"], 1)
        self.assertEqual(diagnostics["skipped_refreshes"]["degraded_mode"], 2)

    async def test_global_rate_limit_paces_reads_and_writes(self) -> None:
        """All outbound API calls should share the same global pacing."""
        api = await self._make_api(
            lambda request: MockResponse(200, json_data={"t": 1})
        )
        api._request_rate_limit = 50
        started_at: list[float] = []

        async def wait_for_slot() -> None:
            await api._wait_for_request_slot()
            started_at.append(asyncio.get_running_loop().time())

        await asyncio.gather(wait_for_slot(), wait_for_slot(), wait_for_slot())

        self.assertEqual(len(started_at), 3)
        self.assertGreaterEqual(started_at[1] - started_at[0], 0.015)
        self.assertGreaterEqual(started_at[2] - started_at[1], 0.015)

    async def test_send_command_accepts_result_shape(self) -> None:
        """Command responses with a result field should be accepted."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices/device-1/commands":
                payload = json.loads(request.content.decode())
                self.assertEqual(
                    payload,
                    {"commands": [{"code": "temp_set", "value": 220}]},
                )
                return MockResponse(201, json_data={"result": True, "t": 4})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        result = await ally.send_command("device-1", [("temp_set", 220)])

        self.assertTrue(result)

    async def test_send_command_accepts_timestamp_only_shape(self) -> None:
        """Command responses with only a timestamp should still be treated as success."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices/device-1/commands":
                return MockResponse(201, json_data={"t": 5})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        self.assertTrue(await ally.set_temperature("device-1", 22.0, code="temp_set"))

    async def test_set_temperature_still_writes_when_cached_value_matches(self) -> None:
        """Explicit temperature writes should still reach the API even if cache matches."""
        command_calls = 0

        def handler(request: MockRequest) -> MockResponse:
            nonlocal command_calls
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(
                    200,
                    json_data={"result": [THERMOSTAT_WITH_MODE_SETPOINTS], "t": 1},
                )
            if request.url.path == "/ally/devices/device-1/commands":
                command_calls += 1
                payload = json.loads(request.content.decode())
                self.assertEqual(
                    payload,
                    {"commands": [{"code": "manual_mode_fast", "value": 215}]},
                )
                return MockResponse(201, json_data={"result": True, "t": 16})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()

        self.assertTrue(
            await ally.set_temperature("device-1", 21.5, code="manual_mode_fast")
        )
        self.assertEqual(command_calls, 1)

    async def test_set_temperature_for_mode_still_writes_when_cache_matches(
        self,
    ) -> None:
        """Mode-aware writes should still send mode and setpoint when explicitly requested."""
        command_payloads: list[dict[str, object]] = []

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(
                    200,
                    json_data={"result": [THERMOSTAT_WITH_MODE_SETPOINTS], "t": 1},
                )
            if request.url.path == "/ally/devices/device-1/commands":
                payload = json.loads(request.content.decode())
                command_payloads.append(payload)
                return MockResponse(201, json_data={"result": True, "t": 17})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()

        self.assertTrue(await ally.set_temperature_for_mode("device-1", 21.5, "manual"))
        self.assertEqual(
            command_payloads,
            [
                {"commands": [{"code": "mode", "value": "manual"}]},
                {"commands": [{"code": "manual_mode_fast", "value": 215}]},
            ],
        )

    async def test_set_temperature_for_mode_sends_full_explicit_flow(self) -> None:
        """Mode-aware writes should send both calls even if cached mode already matches."""
        command_payloads: list[dict[str, object]] = []

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(
                    200,
                    json_data={"result": [THERMOSTAT_WITH_MODE_SETPOINTS], "t": 1},
                )
            if request.url.path == "/ally/devices/device-1/commands":
                payload = json.loads(request.content.decode())
                command_payloads.append(payload)
                if payload == {"commands": [{"code": "mode", "value": "manual"}]}:
                    return MockResponse(201, json_data={"result": True, "t": 6})
                if payload == {
                    "commands": [{"code": "manual_mode_fast", "value": 230}]
                }:
                    return MockResponse(201, json_data={"result": True, "t": 7})
                raise AssertionError(f"Unexpected command payload: {payload}")
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()

        self.assertTrue(await ally.set_temperature_for_mode("device-1", 23.0, "manual"))
        self.assertEqual(
            command_payloads,
            [
                {"commands": [{"code": "mode", "value": "manual"}]},
                {"commands": [{"code": "manual_mode_fast", "value": 230}]},
            ],
        )

    async def test_set_temperature_for_mode_sets_mode_before_mode_setpoint(
        self,
    ) -> None:
        """Mode-aware temperature writes should set the matching mode first."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(
                    200,
                    json_data={"result": [THERMOSTAT_WITH_MODE_SETPOINTS], "t": 1},
                )
            if request.url.path == "/ally/devices/device-1/commands":
                payload = json.loads(request.content.decode())
                if payload == {"commands": [{"code": "mode", "value": "manual"}]}:
                    return MockResponse(201, json_data={"result": True, "t": 6})
                if payload == {
                    "commands": [{"code": "manual_mode_fast", "value": 230}]
                }:
                    return MockResponse(201, json_data={"result": True, "t": 7})
                raise AssertionError(f"Unexpected command payload: {payload}")
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()

        self.assertTrue(await ally.set_temperature_for_mode("device-1", 23.0, "manual"))

    async def test_set_manual_temperature_is_manual_mode_helper(self) -> None:
        """Manual temperature helper should reuse manual mode semantics."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(
                    200,
                    json_data={"result": [THERMOSTAT_WITH_MODE_SETPOINTS], "t": 1},
                )
            if request.url.path == "/ally/devices/device-1/commands":
                payload = json.loads(request.content.decode())
                if payload == {"commands": [{"code": "mode", "value": "manual"}]}:
                    return MockResponse(201, json_data={"result": True, "t": 8})
                if payload == {
                    "commands": [{"code": "manual_mode_fast", "value": 225}]
                }:
                    return MockResponse(201, json_data={"result": True, "t": 9})
                raise AssertionError(f"Unexpected command payload: {payload}")
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()

        self.assertTrue(await ally.set_manual_temperature("device-1", 22.5))

    async def test_temperature_setting_helpers_send_expected_payloads(self) -> None:
        """Bounded temperature helpers should write the matching command code."""
        expected_payloads = [
            {"commands": [{"code": "upper_temp", "value": 350}]},
            {"commands": [{"code": "lower_temp", "value": 50}]},
            {"commands": [{"code": "at_home_setting", "value": 215}]},
            {"commands": [{"code": "leaving_home_setting", "value": 170}]},
            {"commands": [{"code": "holiday_setting", "value": 150}]},
        ]
        command_payloads: list[dict[str, object]] = []

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices/device-1/commands":
                payload = json.loads(request.content.decode())
                command_payloads.append(payload)
                return MockResponse(201, json_data={"result": True, "t": 19})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        self.assertTrue(await ally.set_upper_temp("device-1", 35.0))
        self.assertTrue(await ally.set_lower_temp("device-1", 5.0))
        self.assertTrue(await ally.set_at_home_setting("device-1", 21.5))
        self.assertTrue(await ally.set_leaving_home_setting("device-1", 17.0))
        self.assertTrue(await ally.set_holiday_setting("device-1", 15.0))
        self.assertEqual(command_payloads, expected_payloads)

    async def test_temperature_setting_helpers_validate_range_and_step(self) -> None:
        """Bounded temperature helpers should reject invalid range and step values."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        invalid_calls = [
            (ally.set_upper_temp, 4.5, "between 5.0 and 35.0"),
            (ally.set_lower_temp, 35.5, "between 5.0 and 35.0"),
            (ally.set_at_home_setting, 21.3, "0.5 degree steps"),
            (ally.set_leaving_home_setting, 17.1, "0.5 degree steps"),
            (ally.set_holiday_setting, 15.25, "0.5 degree steps"),
        ]

        for method, value, message in invalid_calls:
            with self.subTest(method=method.__name__, value=value):
                with self.assertRaisesRegex(ValueError, message):
                    await method("device-1", value)

    async def test_set_external_temperature_enables_radiator_covered(self) -> None:
        """External temperature writes should force covered-radiator mode."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices/device-1/commands":
                payload = json.loads(request.content.decode())
                self.assertEqual(
                    payload,
                    {
                        "commands": [
                            {"code": "radiator_covered", "value": True},
                            {"code": "ext_measured_rs", "value": 2500},
                            {"code": "sensor_avg_temp", "value": 250},
                        ]
                    },
                )
                return MockResponse(201, json_data={"result": True, "t": 14})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        self.assertTrue(await ally.set_external_temperature("device-1", 25.0))

    async def test_set_external_temperature_still_writes_when_cached_state_matches(
        self,
    ) -> None:
        """Explicit external temperature writes should still reach the API if cache matches."""
        cached_device = {
            **DEVICE_PAYLOAD,
            "status": [
                {"code": "radiator_covered", "value": True},
                {"code": "ext_measured_rs", "value": 2500},
                {"code": "sensor_avg_temp", "value": 250},
            ],
        }

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(200, json_data={"result": [cached_device], "t": 1})
            if request.url.path == "/ally/devices/device-1/commands":
                payload = json.loads(request.content.decode())
                self.assertEqual(
                    payload,
                    {
                        "commands": [
                            {"code": "radiator_covered", "value": True},
                            {"code": "ext_measured_rs", "value": 2500},
                            {"code": "sensor_avg_temp", "value": 250},
                        ]
                    },
                )
                return MockResponse(201, json_data={"result": True, "t": 18})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()

        self.assertTrue(await ally.set_external_temperature("device-1", 25.0))

    async def test_set_external_temperature_sends_full_payload_when_any_value_changes(
        self,
    ) -> None:
        """Multi-property writes should send the full command list when any value differs."""
        cached_device = {
            **DEVICE_PAYLOAD,
            "status": [
                {"code": "radiator_covered", "value": True},
                {"code": "ext_measured_rs", "value": 2400},
                {"code": "sensor_avg_temp", "value": 240},
            ],
        }

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(200, json_data={"result": [cached_device], "t": 1})
            if request.url.path == "/ally/devices/device-1/commands":
                payload = json.loads(request.content.decode())
                self.assertEqual(
                    payload,
                    {
                        "commands": [
                            {"code": "radiator_covered", "value": True},
                            {"code": "ext_measured_rs", "value": 2500},
                            {"code": "sensor_avg_temp", "value": 250},
                        ]
                    },
                )
                return MockResponse(201, json_data={"result": True, "t": 14})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()

        self.assertTrue(await ally.set_external_temperature("device-1", 25.0))

    async def test_diagnostics_track_unsupported_status(
        self,
    ) -> None:
        """Wrapper diagnostics should expose unsupported status devices."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(
                    200, json_data={"result": [ROOM_SENSOR_PAYLOAD], "t": 1}
                )
            if request.url.path == "/ally/devices/sensor-1/status":
                return MockResponse(500, json_data={"title": "Internal Server Error"})
            if request.url.path == "/ally/devices/sensor-1":
                return MockResponse(
                    200,
                    json_data={"result": ROOM_SENSOR_PAYLOAD, "t": 15},
                )
            if request.url.path == "/ally/devices/device-1/commands":
                return MockResponse(201, json_data={"result": True, "t": 16})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")
        await ally.get_devices()
        await ally.refresh_device("sensor-1")

        ally._store_device(THERMOSTAT_WITH_MODE_SETPOINTS)  # type: ignore[attr-defined]
        await ally.set_temperature("device-1", 21.5, code="manual_mode_fast")

        diagnostics = ally.get_diagnostics()

        self.assertEqual(diagnostics["unsupported_status_device_count"], 1)
        self.assertEqual(diagnostics["unsupported_status_devices"], ["sensor-1"])

    async def test_set_radiator_covered_false_clears_external_temperature(self) -> None:
        """Disabling covered-radiator mode should clear external sensor values."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices/device-1/commands":
                payload = json.loads(request.content.decode())
                self.assertEqual(
                    payload,
                    {
                        "commands": [
                            {"code": "radiator_covered", "value": False},
                            {"code": "ext_measured_rs", "value": -8000},
                            {"code": "sensor_avg_temp", "value": -800},
                        ]
                    },
                )
                return MockResponse(201, json_data={"result": True, "t": 15})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        self.assertTrue(await ally.set_radiator_covered("device-1", False))

    async def test_set_temperature_for_mode_sets_auto_mode_before_auto_setpoint(
        self,
    ) -> None:
        """Mode-aware temperature writes should switch back to schedule mode."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(
                    200,
                    json_data={"result": [THERMOSTAT_WITH_MODE_SETPOINTS], "t": 1},
                )
            if request.url.path == "/ally/devices/device-1/commands":
                payload = json.loads(request.content.decode())
                if payload["commands"][0] == {"code": "mode", "value": "at_home"}:
                    return MockResponse(201, json_data={"result": True, "t": 10})
                if payload == {"commands": [{"code": "at_home_setting", "value": 210}]}:
                    return MockResponse(201, json_data={"result": True, "t": 11})
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

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices/device-1/commands":
                return MockResponse(400, json_data={"title": "Bad Request"})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        with self.assertRaises(BadRequestError):
            await ally.send_command("device-1", [("temp_set", 220)])

    async def test_rate_limit_maps_to_domain_exception(self) -> None:
        """HTTP 429 should map to the dedicated throttling exception."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(429, json_data={"title": "Too Many Requests"})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        with self.assertRaises(RateLimitError):
            await ally.get_devices()

    async def test_forbidden_maps_to_domain_exception(self) -> None:
        """HTTP 403 should map to the dedicated forbidden exception."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(403, json_data={"title": "Forbidden"})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        with self.assertRaises(ForbiddenError):
            await ally.get_devices()

    async def test_server_error_range_maps_to_domain_exception(self) -> None:
        """HTTP 5xx responses should map to the dedicated server exception."""

        def handler(request: MockRequest) -> MockResponse:
            if request.url.path == "/oauth2/token":
                return MockResponse(200, json_data=TOKEN_RESPONSE)
            if request.url.path == "/ally/devices":
                return MockResponse(503, json_data={"title": "Service Unavailable"})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        ally = DanfossAlly(api=await self._make_api(handler))
        await ally.initialize("key", "secret")

        with self.assertRaises(InternalServerError):
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
