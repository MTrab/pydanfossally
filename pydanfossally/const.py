"""Ally specific consts."""

THERMOSTAT_MODE_AUTO = "hot"
THERMOSTAT_MODE_MANUAL = "manual"
THERMOSTAT_MODE_OFF = "pause"

SETPOINT_CODES = {
    "manual_mode_fast",
    "at_home_setting",
    "leaving_home_setting",
    "pause_setting",
    "holiday_setting",
    "temp_set",
}
BOOLEAN_CODES = {
    "window_toggle",
    "switch",
    "switch_state",
    "heat_supply_request",
    "mounting_mode_active",
    "heat_available",
    "load_balance_enable",
    "radiator_covered",
}
PASSTHROUGH_CODES = {
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

MODE_TO_SETPOINT_CODE = {
    "at_home": "at_home_setting",
    "home": "at_home_setting",
    "leaving_home": "leaving_home_setting",
    "away": "leaving_home_setting",
    "pause": "pause_setting",
    "manual": "manual_mode_fast",
    "holiday": "holiday_setting",
    "holiday_sat": "at_home_setting",
}

REFRESH_DEVICE_CONCURRENCY = 5
REFRESH_DEVICE_MIN_INTERVAL = 0.10
DEVICE_DISCOVERY_INTERVAL = 3600.0
