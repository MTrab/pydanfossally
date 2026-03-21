"""Async-first module for handling Danfoss Ally API communication."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
import logging
import re
import time
from typing import Any

from .const import (
    BOOLEAN_CODES,
    DEGRADED_REFRESH_COOLDOWN,
    DEVICE_DISCOVERY_INTERVAL,
    LOW_PRIORITY_DEVICE_TYPES,
    MODE_TO_SETPOINT_CODE,
    PASSTHROUGH_CODES,
    REFRESH_DEVICE_CONCURRENCY,
    REFRESH_DEVICE_MIN_INTERVAL,
    SETPOINT_CODES,
)
from .danfossallyapi import (
    DEFAULT_TIMEOUT,
    DIAGNOSTIC_WINDOW_SECONDS,
    DanfossAllyAPI,
)
from .exceptions import InternalServerError, RateLimitError

_LOGGER = logging.getLogger(__name__)

__all__ = ["DanfossAlly", "DanfossAllyAPI", "parse_device_data"]


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
            if code in SETPOINT_CODES:
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
            elif code in BOOLEAN_CODES:
                normalized = _normalize_bool(value)
                if normalized is not None:
                    parsed[code.lower()] = normalized

            if code in PASSTHROUGH_CODES:
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


def _copy_status_entries(statuses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create a detached copy of device status entries."""
    return [dict(status) for status in statuses]


def _normalize_device_type(value: Any) -> str:
    """Normalize device types for stable priority lookups."""
    if not isinstance(value, str):
        return ""
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return " ".join(normalized.split())


def _values_match(current: Any, expected: Any) -> bool:
    """Compare cached and desired values with light numeric tolerance."""
    if isinstance(current, bool) or isinstance(expected, bool):
        return current is expected
    if isinstance(current, (int, float)) and isinstance(expected, (int, float)):
        return abs(float(current) - float(expected)) < 0.001
    return current == expected


class DanfossAlly:
    """Async-first Danfoss Ally API connector."""

    def __init__(
        self,
        api: DanfossAllyAPI | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        refresh_device_concurrency: int = REFRESH_DEVICE_CONCURRENCY,
        refresh_device_min_interval: float = REFRESH_DEVICE_MIN_INTERVAL,
        device_discovery_interval: float = DEVICE_DISCOVERY_INTERVAL,
        degraded_refresh_cooldown: float = DEGRADED_REFRESH_COOLDOWN,
        user_agent_prefix: str | None = None,
    ) -> None:
        """Initialize the connector."""
        if refresh_device_concurrency < 1:
            raise ValueError("refresh_device_concurrency must be at least 1")
        if refresh_device_min_interval < 0:
            raise ValueError("refresh_device_min_interval must be non-negative")
        if device_discovery_interval < 0:
            raise ValueError("device_discovery_interval must be non-negative")
        if degraded_refresh_cooldown < 0:
            raise ValueError("degraded_refresh_cooldown must be non-negative")

        self._authorized = False
        self._token: str | None = None
        self.devices: dict[str, dict[str, Any]] = {}
        self._device_metadata: dict[str, dict[str, Any]] = {}
        self._api = api or DanfossAllyAPI(
            timeout=timeout,
            user_agent_prefix=user_agent_prefix,
        )
        self._refresh_device_concurrency = refresh_device_concurrency
        self._refresh_device_min_interval = refresh_device_min_interval
        self._device_discovery_interval = device_discovery_interval
        self._degraded_refresh_cooldown = degraded_refresh_cooldown
        self._diagnostic_window = DIAGNOSTIC_WINDOW_SECONDS
        self._refresh_rate_lock = asyncio.Lock()
        self._next_refresh_slot = 0.0
        self._next_device_discovery_at = 0.0
        self._degraded_refresh_until = 0.0
        self._status_refresh_recovery_limit: int | None = None
        self._status_refresh_unsupported: dict[str, float] = {}
        self._skipped_refresh_times: dict[str, deque[float]] = defaultdict(deque)
        self._degraded_mode_entry_times: deque[float] = deque()
        self._skipped_write_times: deque[float] = deque()

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
        _LOGGER.debug("Initializing DanfossAlly client")
        token = await self._api.get_token(key, secret)

        if token is False:
            self._authorized = False
            _LOGGER.error("Error in authorization")
            return False

        self._token = self._api.token
        self._authorized = True
        _LOGGER.debug("DanfossAlly client initialized successfully")
        return True

    async def get_devices(self) -> dict[str, dict[str, Any]]:
        """Fetch and parse all devices."""
        _LOGGER.debug("Fetching bulk device snapshot")
        response = await self._api.get_devices()
        if not response or "result" not in response:
            _LOGGER.error("Something went wrong loading devices")
            return self.devices

        bulk_response_time = response.get("t")
        previous_devices = self.devices
        previous_metadata = self._device_metadata
        self.devices = {}
        self._device_metadata = {}
        for device in response["result"]:
            device_id = device["id"]
            cached_device = previous_devices.get(device_id)
            cached_metadata = previous_metadata.get(device_id)
            cached_response_time = (
                cached_device.get("last_response_time") if cached_device else None
            )

            if (
                bulk_response_time is not None
                and isinstance(cached_response_time, (int, float))
                and cached_response_time > bulk_response_time
                and cached_metadata is not None
            ):
                self.devices[device_id] = cached_device.copy()
                self._device_metadata[device_id] = dict(cached_metadata)
                _LOGGER.debug(
                    "Keeping cached device %s because cached t=%s is newer than bulk t=%s",
                    device_id,
                    cached_response_time,
                    bulk_response_time,
                )
                self._record_skipped_refresh("stale_bulk_device")
                continue

            self._store_device(device, last_response_time=bulk_response_time)

        self._schedule_next_device_discovery()

        _LOGGER.debug(
            "Loaded %s devices from bulk snapshot with t=%s",
            len(self.devices),
            response.get("t"),
        )
        self._log_diagnostics("bulk device snapshot")
        return self.devices

    async def get_device(self, device_id: str) -> dict[str, Any] | None:
        """Fetch one device and parse it into the cached device map."""
        _LOGGER.debug("Fetching realtime state for device %s", device_id)
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
        """Refresh one cached device via status or device endpoints."""
        if not self._should_use_status_refresh(device_id):
            return await self.get_device(device_id)

        try:
            return await self.refresh_device_status(device_id)
        except InternalServerError:
            _LOGGER.debug(
                "Status refresh is unsupported for device %s; falling back to full device reads",
                device_id,
            )
            self._status_refresh_unsupported[device_id] = time.monotonic()
            self._log_diagnostics("unsupported status refresh fallback")
            return await self.get_device(device_id)

    async def refresh_device_status(self, device_id: str) -> dict[str, Any] | None:
        """Refresh one cached device via the lighter status endpoint."""
        metadata = self._device_metadata.get(device_id)
        if metadata is None:
            _LOGGER.debug(
                "Status refresh requested without cached metadata for device %s; loading full device",
                device_id,
            )
            return await self.get_device(device_id)

        _LOGGER.debug("Fetching status-only refresh for device %s", device_id)
        response = await self._api.get_device_status(device_id)
        statuses = response.get("result", [])
        synthetic_device = self._merge_status_into_metadata(device_id, statuses)
        return self._store_device(
            synthetic_device,
            last_response_time=response.get("t"),
            preserve_metadata=True,
        )

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
        """Refresh cached devices with hybrid bulk and per-device reads."""
        if not self.devices:
            _LOGGER.debug(
                "Refresh requested without cached devices; loading bulk snapshot for discovery",
            )
            return await self.get_devices()

        if self._should_refresh_device_discovery():
            _LOGGER.debug(
                "Device discovery interval elapsed; refreshing bulk device snapshot"
            )
            await self.get_devices()

        device_ids = self._get_high_priority_refresh_candidates()
        if not device_ids:
            self._record_skipped_refresh("no_high_priority_candidates")
            _LOGGER.debug("No high-priority devices eligible for per-device refresh")
            return self.devices

        if self._is_degraded_refresh_active():
            self._record_skipped_refresh("degraded_mode", len(device_ids))
            _LOGGER.debug(
                "Per-device refreshes are in cooldown until %.3f; returning bulk-backed cache only",
                self._degraded_refresh_until,
            )
            return self.devices

        recovery_limit = self._status_refresh_recovery_limit
        if recovery_limit is not None:
            device_ids = device_ids[:recovery_limit]

        semaphore = asyncio.Semaphore(self._refresh_device_concurrency)
        rate_limited = False

        async def refresh_one(device_id: str) -> None:
            nonlocal rate_limited
            async with semaphore:
                if rate_limited:
                    self._record_skipped_refresh("rate_limited_remainder")
                    return
                await self._wait_for_refresh_slot()
                try:
                    await self.refresh_device(device_id)
                except RateLimitError:
                    rate_limited = True
                    self._enter_degraded_refresh_mode()

        await asyncio.gather(*(refresh_one(device_id) for device_id in device_ids))

        if not rate_limited:
            self._advance_status_refresh_recovery(
                len(self._get_high_priority_refresh_candidates())
            )

        _LOGGER.debug(
            "Refreshed %s high-priority device(s) via hybrid endpoint selection",
            len(device_ids),
        )
        return self.devices

    def _should_refresh_device_discovery(self) -> bool:
        """Return whether periodic bulk discovery should run before device refresh."""
        return asyncio.get_running_loop().time() >= self._next_device_discovery_at

    def _schedule_next_device_discovery(self) -> None:
        """Schedule the next bulk device discovery pass."""
        self._next_device_discovery_at = (
            asyncio.get_running_loop().time() + self._device_discovery_interval
        )

    def _store_device_metadata(self, device: dict[str, Any]) -> dict[str, Any]:
        """Cache the raw device payload for future status merges."""
        metadata = dict(device)
        statuses = metadata.get("status")
        if isinstance(statuses, list):
            metadata["status"] = _copy_status_entries(statuses)
        device_id = metadata["id"]
        self._device_metadata[device_id] = metadata
        return metadata

    def _merge_status_into_metadata(
        self,
        device_id: str,
        statuses: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Merge fresh status entries into cached metadata for parser reuse."""
        metadata = self._device_metadata[device_id]
        merged = dict(metadata)
        merged["status"] = _copy_status_entries(statuses)
        self._device_metadata[device_id] = merged
        return merged

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
        device = self.devices.get(device_id)
        if device and self._cached_command_matches(device, "mode", mode):
            self._record_timestamp(self._skipped_write_times)
            _LOGGER.debug(
                "Skipping mode update for device %s because cached mode already matches %s",
                device_id,
                mode,
            )
            return True
        _LOGGER.debug("Setting mode for device %s to %s", device_id, mode)
        return await self._api.set_mode(device_id, mode)

    async def send_command(
        self,
        device_id: str,
        listofcommands: list[tuple[str, Any]],
    ) -> bool:
        """Send generic commands for one device."""
        if self._should_skip_cached_command_call(device_id, listofcommands):
            self._record_timestamp(self._skipped_write_times)
            _LOGGER.debug(
                "Skipping command(s) for device %s because cached state already matches",
                device_id,
            )
            return True

        _LOGGER.debug(
            "Sending generic command(s) for device %s with codes=%s",
            device_id,
            [code for code, _ in listofcommands],
        )
        return await self._api.send_command(device_id, listofcommands)

    def _store_device(
        self,
        device: dict[str, Any],
        *,
        last_response_time: int | None = None,
        preserve_metadata: bool = False,
    ) -> dict[str, Any]:
        """Parse and cache one device payload."""
        if preserve_metadata:
            statuses = device.get("status")
            if isinstance(statuses, list):
                self._device_metadata[device["id"]]["status"] = _copy_status_entries(
                    statuses
                )
        else:
            device = self._store_device_metadata(device)

        parsed = parse_device_data(device, last_response_time=last_response_time)
        device_id = device["id"]
        self.devices[device_id] = parsed
        _LOGGER.debug("Stored parsed state for device %s", device_id)
        return parsed

    def _get_high_priority_refresh_candidates(self) -> list[str]:
        """Return devices that should receive per-device refreshes."""
        return [
            device_id
            for device_id in self.devices
            if self._is_high_priority_device(device_id)
        ]

    def _should_skip_cached_command_call(
        self,
        device_id: str,
        commands: list[tuple[str, Any]],
    ) -> bool:
        """Return whether all commands already match the cached device state."""
        device = self.devices.get(device_id)
        if not device:
            return False
        for code, value in commands:
            if not self._cached_command_matches(device, code, value):
                return False
        for code, value in commands:
            _LOGGER.debug(
                "Skipping command %s for device %s because cached value already matches %s",
                code,
                device_id,
                value,
            )
        return True

    def _cached_command_matches(
        self,
        device: dict[str, Any],
        code: str,
        value: Any,
    ) -> bool:
        """Return whether a command already matches the cached parsed state."""
        expected_key = code.lower()
        expected_value = value

        if code in SETPOINT_CODES:
            expected_value = float(value) / 10
        elif code in BOOLEAN_CODES:
            expected_value = _normalize_bool(value)
        elif code == "ext_measured_rs":
            expected_value = float(value) / 100
        elif code == "sensor_avg_temp":
            expected_key = "external_sensor_temperature"
            expected_value = float(value) / 10
        elif code != "mode":
            return False

        if expected_value is None or expected_key not in device:
            return False
        return _values_match(device[expected_key], expected_value)

    def _record_skipped_refresh(self, reason: str, count: int = 1) -> None:
        """Count refreshes that were skipped by policy or cooldown."""
        timestamps = self._skipped_refresh_times[reason]
        for _ in range(count):
            self._record_timestamp(timestamps)

    def _record_timestamp(self, timestamps: deque[float]) -> None:
        """Record one diagnostics event in the rolling window."""
        timestamps.append(time.monotonic())
        self._prune_timestamps(timestamps)

    def _prune_timestamps(self, timestamps: deque[float]) -> None:
        """Drop diagnostics entries that fall outside the rolling window."""
        cutoff = time.monotonic() - self._diagnostic_window
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

    def _is_high_priority_device(self, device_id: str) -> bool:
        """Classify devices that deserve near-realtime refreshes."""
        parsed = self.devices.get(device_id, {})
        if parsed.get("isThermostat"):
            return True

        metadata = self._device_metadata.get(device_id, {})
        model = str(
            metadata.get("model")
            or metadata.get("device_type")
            or parsed.get("model")
            or ""
        ).lower()
        normalized_device_type = _normalize_device_type(
            metadata.get("device_type") or metadata.get("model") or parsed.get("model")
        )

        if normalized_device_type in LOW_PRIORITY_DEVICE_TYPES:
            return False

        if "gateway" in model or "controller" in model:
            return False
        if "sensor" in model:
            return True
        return False

    def _should_use_status_refresh(self, device_id: str) -> bool:
        """Return whether status refresh should be attempted for one device."""
        return (
            device_id in self._device_metadata
            and self._is_high_priority_device(device_id)
            and device_id not in self._status_refresh_unsupported
        )

    def _is_degraded_refresh_active(self) -> bool:
        """Return whether per-device refreshes are temporarily disabled."""
        return asyncio.get_running_loop().time() < self._degraded_refresh_until

    def _enter_degraded_refresh_mode(self) -> None:
        """Disable per-device refreshes until the cooldown elapses."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        self._degraded_refresh_until = (
            max(now, self._degraded_refresh_until) + self._degraded_refresh_cooldown
        )
        self._status_refresh_recovery_limit = 1
        self._record_timestamp(self._degraded_mode_entry_times)
        _LOGGER.debug(
            "Entering degraded refresh mode until %.3f after rate limiting",
            self._degraded_refresh_until,
        )
        self._log_diagnostics("degraded refresh entry")

    def _advance_status_refresh_recovery(self, total_candidates: int) -> None:
        """Gradually restore per-device refresh breadth after cooldown."""
        if self._status_refresh_recovery_limit is None or total_candidates < 1:
            return
        if self._is_degraded_refresh_active():
            return
        if self._status_refresh_recovery_limit >= total_candidates:
            self._status_refresh_recovery_limit = None
            return
        self._status_refresh_recovery_limit = min(
            total_candidates,
            self._status_refresh_recovery_limit * 2,
        )

    def _get_setpoint_code_for_mode(self, device: dict[str, Any], mode: str) -> str:
        """Resolve the Danfoss setpoint field for one mode."""
        if _uses_temp_set_fallback(device):
            return "temp_set"

        return MODE_TO_SETPOINT_CODE.get(mode, "manual_mode_fast")

    @property
    def authorized(self) -> bool:
        """Return authorization state."""
        return self._authorized

    @property
    def token(self) -> str | None:
        """Return the cached bearer token."""
        return self._token

    def get_diagnostics(self) -> dict[str, Any]:
        """Return lightweight diagnostics for refresh tuning and debugging."""
        skipped_refreshes: dict[str, int] = {}
        for reason, timestamps in self._skipped_refresh_times.items():
            self._prune_timestamps(timestamps)
            if timestamps:
                skipped_refreshes[reason] = len(timestamps)

        self._prune_timestamps(self._degraded_mode_entry_times)
        self._prune_timestamps(self._skipped_write_times)

        cutoff = time.monotonic() - self._diagnostic_window
        unsupported_status_devices = sorted(
            device_id
            for device_id, seen_at in self._status_refresh_unsupported.items()
            if seen_at >= cutoff
        )
        return {
            "request_counts": self._api.get_diagnostics()["request_counts"],
            "skipped_refreshes": dict(sorted(skipped_refreshes.items())),
            "degraded_mode_entries": len(self._degraded_mode_entry_times),
            "unsupported_status_devices": unsupported_status_devices,
            "unsupported_status_device_count": len(unsupported_status_devices),
            "skipped_write_calls": len(self._skipped_write_times),
        }

    def _log_diagnostics(self, context: str) -> None:
        """Emit a compact diagnostics snapshot in debug logs."""
        diagnostics = self.get_diagnostics()
        _LOGGER.debug(
            "Diagnostics after %s (last %sm)",
            context,
            int(self._diagnostic_window // 60),
        )
        for endpoint, count in diagnostics["request_counts"].items():
            _LOGGER.debug("  requests.%s=%s", endpoint, count)
        for reason, count in diagnostics["skipped_refreshes"].items():
            _LOGGER.debug("  skipped_refreshes.%s=%s", reason, count)
        _LOGGER.debug(
            "  degraded_mode_entries=%s",
            diagnostics["degraded_mode_entries"],
        )
        _LOGGER.debug(
            "  unsupported_status_device_count=%s",
            diagnostics["unsupported_status_device_count"],
        )
        _LOGGER.debug(
            "  unsupported_status_devices=%s",
            ", ".join(diagnostics["unsupported_status_devices"]),
        )
        _LOGGER.debug(
            "  skipped_write_calls=%s",
            diagnostics["skipped_write_calls"],
        )
