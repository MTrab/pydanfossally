"""Async-first module for handling Danfoss Ally API communication."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any

from .danfossallyapi import DEFAULT_TIMEOUT, DanfossAllyAPI

_LOGGER = logging.getLogger(__name__)

__all__ = ["DanfossAlly", "DanfossAllyAPI", "parse_device_data"]

_SETPOINT_CODES = {
    "manual_mode_fast",
    "at_home_setting",
    "leaving_home_setting",
    "pause_setting",
    "holiday_setting",
    "temp_set",
}
_BOOLEAN_CODES = {
    "window_toggle",
    "switch",
    "switch_state",
    "heat_supply_request",
    "mounting_mode_active",
    "heat_available",
    "load_balance_enable",
    "radiator_covered",
}
_PASSTHROUGH_CODES = {
    "child_lock",
    "mode",
    "work_state",
    "load_balance_enable",
    "fault",
    "sw_error_code",
    "ctrl_alg",
    "adaptation_runstatus",
    "SetpointChangeSource",
}

_MODE_TO_SETPOINT_CODE = {
    "at_home": "at_home_setting",
    "home": "at_home_setting",
    "leaving_home": "leaving_home_setting",
    "away": "leaving_home_setting",
    "pause": "pause_setting",
    "manual": "manual_mode_fast",
    "holiday": "holiday_setting",
    "holiday_sat": "at_home_setting",
}
_REFRESH_DEVICE_CONCURRENCY = 2
_REFRESH_DEVICE_MIN_INTERVAL = 0.35
_REFRESH_DEVICE_TIMEOUT = 600.0


@dataclass
class _PendingDeviceSync:
    """Track one device that should keep using realtime refresh temporarily."""

    expected_fields: dict[str, Any]
    deadline: float


def _normalize_bool(value: Any) -> bool | None:
    """Convert API booleans that may arrive as bools or strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return None


def parse_device_data(
    device: dict[str, Any],
    *,
    last_response_time: int | None = None,
) -> dict[str, Any]:
    """Convert raw API device payloads into the library's best-effort model."""
    parsed: dict[str, Any] = {
        "isThermostat": False,
        "name": device.get("name", "").strip(),
        "online": device.get("online", False),
        "update": device.get("update_time"),
        "floor_sensor": False,
    }
    if last_response_time is not None:
        parsed["last_response_time"] = last_response_time

    if "model" in device:
        parsed["model"] = device["model"]
    elif "device_type" in device:
        parsed["model"] = device["device_type"]

    statuses = device.get("status") or []
    for status in statuses:
        if status.get("code") == "floor_sensor":
            parsed["floor_sensor"] = bool(status.get("value"))
            break

    for status in statuses:
        code = status.get("code")
        value = status.get("value")

        try:
            if code in _SETPOINT_CODES:
                parsed[code] = float(value) / 10
                parsed["isThermostat"] = True
            elif code == "temp_current":
                parsed["temperature"] = float(value) / 10
            elif code == "MeasuredValue" and parsed["floor_sensor"]:
                parsed["floor_temperature"] = float(value) / 10
            elif code in {
                "upper_temp",
                "lower_temp",
                "floor_temp_min",
                "floor_temp_max",
            }:
                parsed[code] = float(value) / 10
            elif code in {"local_temperature", "ext_measured_rs"}:
                parsed[code] = float(value) / 100
            elif code == "humidity_value":
                parsed["humidity"] = float(value) / 10
            elif code == "battery_percentage":
                parsed["battery"] = value
            elif code == "window_state":
                parsed["window_open"] = value == "open"
            elif code == "output_status":
                parsed["output_status"] = value == "active"
            elif code == "pi_heating_demand":
                parsed["valve_opening"] = value
            elif code == "LoadRadiatorRoomMean":
                parsed["load_room_mean"] = value
            elif code == "sensor_avg_temp":
                parsed["external_sensor_temperature"] = float(value) / 10
            elif code in _BOOLEAN_CODES:
                normalized = _normalize_bool(value)
                if normalized is not None:
                    parsed[code.lower()] = normalized

            if code in _PASSTHROUGH_CODES:
                parsed[code.lower()] = value
        except (AttributeError, KeyError, TypeError, ValueError, IndexError) as err:
            _LOGGER.debug(
                "Failed to handle data for device %s, status code %s: %s",
                device.get("id"),
                code,
                err,
            )

    return parsed


def _uses_temp_set_fallback(device: dict[str, Any]) -> bool:
    """Return whether the device only exposes a single temp_set value."""
    return "temp_set" in device and not {
        "manual_mode_fast",
        "at_home_setting",
        "leaving_home_setting",
        "pause_setting",
        "holiday_setting",
    }.issubset(device)


class DanfossAlly:
    """Async-first Danfoss Ally API connector."""

    def __init__(
        self,
        api: DanfossAllyAPI | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        refresh_device_concurrency: int = _REFRESH_DEVICE_CONCURRENCY,
        refresh_device_min_interval: float = _REFRESH_DEVICE_MIN_INTERVAL,
        refresh_device_timeout: float = _REFRESH_DEVICE_TIMEOUT,
    ) -> None:
        """Initialize the connector."""
        if refresh_device_concurrency < 1:
            raise ValueError("refresh_device_concurrency must be at least 1")
        if refresh_device_min_interval < 0:
            raise ValueError("refresh_device_min_interval must be non-negative")
        if refresh_device_timeout < 0:
            raise ValueError("refresh_device_timeout must be non-negative")

        self._authorized = False
        self._token: str | None = None
        self.devices: dict[str, dict[str, Any]] = {}
        self._api = api or DanfossAllyAPI(timeout=timeout)
        self._refresh_device_concurrency = refresh_device_concurrency
        self._refresh_device_min_interval = refresh_device_min_interval
        self._refresh_device_timeout = refresh_device_timeout
        self._refresh_rate_lock = asyncio.Lock()
        self._next_refresh_slot = 0.0
        self._pending_device_syncs: dict[str, _PendingDeviceSync] = {}
        self._last_bulk_response_time: int | None = None

    async def __aenter__(self) -> DanfossAlly:
        """Allow async context manager usage."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the underlying API client."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close open resources."""
        await self._api.aclose()

    async def initialize(self, key: str, secret: str) -> bool:
        """Authorize and initialize the connection."""
        token = await self._api.get_token(key, secret)

        if token is False:
            self._authorized = False
            _LOGGER.error("Error in authorization")
            return False

        self._token = self._api.token
        self._authorized = True
        return True

    async def get_devices(self) -> dict[str, dict[str, Any]]:
        """Fetch and parse all devices."""
        response = await self._api.get_devices()
        if not response or "result" not in response:
            _LOGGER.error("Something went wrong loading devices")
            return self.devices

        previous_bulk_response_time = self._last_bulk_response_time
        current_bulk_response_time = response.get("t")

        self.devices = {}
        for device in response["result"]:
            self._store_device(device, last_response_time=response.get("t"))

        self._last_bulk_response_time = current_bulk_response_time
        if (
            current_bulk_response_time is None
            or current_bulk_response_time != previous_bulk_response_time
        ):
            self._clear_pending_device_syncs_from_bulk_snapshot()

        return self.devices

    async def get_device(self, device_id: str) -> dict[str, Any] | None:
        """Fetch one device and parse it into the cached device map."""
        response = await self._api.get_device(device_id)
        if not response or "result" not in response:
            _LOGGER.error("Something went wrong loading device %s", device_id)
            return None

        return self._store_device(
            response["result"],
            last_response_time=response.get("t"),
        )

    async def get_device_sub_devices(self, device_id: str) -> list[dict[str, Any]]:
        """Fetch the spec-defined sub-devices endpoint."""
        response = await self._api.get_device_sub_devices(device_id)
        return response.get("result", [])

    async def get_device_status(self, device_id: str) -> list[dict[str, Any]]:
        """Fetch the latest raw status entries for one device."""
        response = await self._api.get_device_status(device_id)
        return response.get("result", [])

    async def refresh_device(self, device_id: str) -> dict[str, Any] | None:
        """Refresh one cached device via its dedicated device endpoint."""
        return await self.get_device(device_id)

    async def _wait_for_refresh_slot(self) -> None:
        """Space refreshes out so the API does not receive a burst of device reads."""
        loop = asyncio.get_running_loop()

        async with self._refresh_rate_lock:
            now = loop.time()
            scheduled_at = max(now, self._next_refresh_slot)
            self._next_refresh_slot = scheduled_at + self._refresh_device_min_interval

        delay = scheduled_at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)

    async def refresh_devices(self) -> dict[str, dict[str, Any]]:
        """Refresh the bulk snapshot, then keep pending devices on the realtime path."""
        await self.get_devices()

        device_ids = self._active_pending_device_ids()
        if not device_ids:
            return self.devices

        semaphore = asyncio.Semaphore(self._refresh_device_concurrency)

        async def refresh_one(device_id: str) -> None:
            async with semaphore:
                await self._wait_for_refresh_slot()
                await self.refresh_device(device_id)

        await asyncio.gather(*(refresh_one(device_id) for device_id in device_ids))

        return self.devices

    async def set_temperature(
        self,
        device_id: str,
        temp: float,
        code: str = "manual_mode_fast",
    ) -> bool:
        """Update temperature setpoint for one device."""
        return await self.send_command(device_id, [(code, int(temp * 10))])

    async def set_temperature_for_mode(
        self,
        device_id: str,
        temp: float,
        mode: str,
    ) -> bool:
        """Apply a temperature for a specific Danfoss mode."""
        device = self.devices.get(device_id, {})
        setpoint_code = self._get_setpoint_code_for_mode(device, mode)

        mode_result = await self.set_mode(device_id, mode)
        if mode_result is False:
            return False

        return await self.set_temperature(device_id, temp, code=setpoint_code)

    async def set_manual_temperature(self, device_id: str, temp: float) -> bool:
        """Apply a manual temperature override for one device."""
        return await self.set_temperature_for_mode(device_id, temp, "manual")

    async def set_external_temperature(
        self,
        device_id: str,
        temperature: float | None,
    ) -> bool:
        """Set an external sensor temperature and enable covered-radiator mode."""
        temp_10 = -800 if temperature is None else int(round(temperature * 10))
        temp_100 = -8000 if temperature is None else int(round(temperature * 100))
        return await self.send_command(
            device_id,
            [
                ("radiator_covered", True),
                ("ext_measured_rs", temp_100),
                ("sensor_avg_temp", temp_10),
            ],
        )

    async def set_radiator_covered(self, device_id: str, covered: bool) -> bool:
        """Set covered-radiator mode and clear external temperature when disabling it."""
        commands: list[tuple[str, Any]] = [("radiator_covered", covered)]
        if not covered:
            commands.extend(
                [
                    ("ext_measured_rs", -8000),
                    ("sensor_avg_temp", -800),
                ]
            )
        return await self.send_command(device_id, commands)

    async def set_mode(self, device_id: str, mode: str) -> bool:
        """Update operating mode for one device."""
        result = await self._api.set_mode(device_id, mode)
        if result:
            self._mark_pending_device_sync(device_id, {"mode": mode})
        return result

    async def send_command(
        self,
        device_id: str,
        listofcommands: list[tuple[str, Any]],
    ) -> bool:
        """Send generic commands for one device."""
        result = await self._api.send_command(device_id, listofcommands)
        if result:
            self._mark_pending_device_sync(
                device_id,
                self._expected_fields_from_commands(listofcommands),
            )
        return result

    def _store_device(
        self,
        device: dict[str, Any],
        *,
        last_response_time: int | None = None,
    ) -> dict[str, Any]:
        """Parse and cache one device payload."""
        parsed = parse_device_data(device, last_response_time=last_response_time)
        device_id = device["id"]
        self.devices[device_id] = parsed
        return parsed

    def _get_setpoint_code_for_mode(self, device: dict[str, Any], mode: str) -> str:
        """Resolve the Danfoss setpoint field for one mode."""
        if _uses_temp_set_fallback(device):
            return "temp_set"

        return _MODE_TO_SETPOINT_CODE.get(mode, "manual_mode_fast")

    def _active_pending_device_ids(self) -> list[str]:
        """Return pending devices that have not yet timed out."""
        now = asyncio.get_running_loop().time()
        expired_device_ids = [
            device_id
            for device_id, pending_sync in self._pending_device_syncs.items()
            if pending_sync.deadline <= now
        ]
        for device_id in expired_device_ids:
            self._pending_device_syncs.pop(device_id, None)
        return list(self._pending_device_syncs)

    def _clear_pending_device_syncs_from_bulk_snapshot(self) -> None:
        """Stop realtime refresh once the bulk endpoint has caught up."""
        satisfied_device_ids = [
            device_id
            for device_id, pending_sync in self._pending_device_syncs.items()
            if self._device_matches_expected_fields(
                self.devices.get(device_id, {}),
                pending_sync.expected_fields,
            )
        ]
        for device_id in satisfied_device_ids:
            self._pending_device_syncs.pop(device_id, None)

    def _mark_pending_device_sync(
        self,
        device_id: str,
        expected_fields: dict[str, Any],
    ) -> None:
        """Track one device until the bulk endpoint reflects the written fields."""
        if not expected_fields:
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._refresh_device_timeout
        current = self._pending_device_syncs.get(device_id)
        merged_fields = dict(current.expected_fields) if current else {}
        merged_fields.update(expected_fields)
        self._pending_device_syncs[device_id] = _PendingDeviceSync(
            expected_fields=merged_fields,
            deadline=deadline,
        )

    def _expected_fields_from_commands(
        self,
        commands: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        """Map write commands onto parsed device fields used for bulk-sync matching."""
        expected_fields: dict[str, Any] = {}
        for code, value in commands:
            if code in _SETPOINT_CODES:
                expected_fields[code] = float(value) / 10
            elif code == "mode":
                expected_fields["mode"] = value
            elif code == "sensor_avg_temp":
                expected_fields["external_sensor_temperature"] = float(value) / 10
            elif code in {"local_temperature", "ext_measured_rs"}:
                expected_fields[code] = float(value) / 100
            elif code in _BOOLEAN_CODES:
                normalized = _normalize_bool(value)
                expected_fields[code.lower()] = value if normalized is None else normalized
            elif code in _PASSTHROUGH_CODES:
                expected_fields[code.lower()] = value
        return expected_fields

    def _device_matches_expected_fields(
        self,
        device: dict[str, Any],
        expected_fields: dict[str, Any],
    ) -> bool:
        """Return whether the parsed bulk device state reflects the expected fields."""
        return all(device.get(field) == value for field, value in expected_fields.items())

    @property
    def authorized(self) -> bool:
        """Return authorization state."""
        return self._authorized

    @property
    def token(self) -> str | None:
        """Return the cached bearer token."""
        return self._token
