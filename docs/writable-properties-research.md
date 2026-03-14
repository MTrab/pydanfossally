# Writable Properties Research

## Summary

This note documents live API probing against two separate Danfoss environments:

- one account with a `Danfoss Ally(TM) Radiator Thermostat`
- one account with `Danfoss Icon2 RT` floor thermostats

The goal was to replace guesswork with observed write semantics for the commands and status codes
that the API actually exposes on these device models.

All live write tests were performed against one representative device per model, with:

- baseline read via `GET /devices/{device_id}`
- one command at a time
- readback after the write
- immediate rollback to the original value
- final readback after rollback
- retry on timeout, using longer API timeouts than the default library timeout

Secrets are intentionally omitted. Device IDs below are shortened.

## Scope And Method

The Danfoss OpenAPI file documents the generic `POST /devices/{device_id}/commands` payload shape,
but it does not document which command codes are valid per device type. This research therefore
relies on live observation.

The primary baseline source was `GET /devices/{device_id}` because it includes the device `status`
array and proved more reliable than `GET /devices/{device_id}/status` during testing.

Observed API quirk:

- the Ally gateway `GET /devices/{device_id}/status` returned `500 Internal Server Error`

Classification used in this document:

- `confirmed writable`: API accepted the command and readback changed as expected
- `api-accepted/no-proof`: API accepted the command, but no reliable effect was observed
- `rejected`: API rejected the command
- `unsafe/deferred`: plausible candidate, but not tested in this pass

## Device Inventory

### Account A: Ally radiator environment

- `bf7d6d...j18s`: `Thermostat Entre`, model `Danfoss Ally(TM) Radiator Thermostat`
- `bfabbe...e2b0`: `Danfoss Ally(TM) Gateway`, model `Danfoss Ally(TM) Gateway`

Representative write-test device:

- `bf7d6d...j18s`

Observed status codes on the representative thermostat:

- `switch`
- `mode`
- `work_state`
- `temp_set`
- `upper_temp`
- `temp_current`
- `window_state`
- `lower_temp`
- `child_lock`
- `battery_percentage`
- `fault`
- `SetpointChangeSource`
- `manual_mode_fast`
- `window_toggle`
- `window_state_info`
- `at_home_setting`
- `leaving_home_setting`
- `pause_setting`
- `holiday_setting`
- `switch_state`
- `mounting_mode_active`
- `ext_measured_rs`
- `radiator_covered`
- `heat_available`
- `load_balance_enable`
- `pi_heating_demand`
- `OccupiedSetpoint`

### Account B: Icon2 floor heating environment

- multiple devices of model `Danfoss Icon2 RT`
- one `Danfoss Icon2 Controller`
- one `Danfoss Ally(TM) Gateway`

Representative write-test device:

- `bfe15e...fffr`: `Entrance Thermostat`, model `Danfoss Icon2 RT`

Observed parsed/device keys across Icon2 RT devices:

- `temp_set`
- `manual_mode_fast`
- `at_home_setting`
- `leaving_home_setting`
- `pause_setting`
- `holiday_setting`
- `mode`
- `upper_temp`
- `lower_temp`
- `floor_temperature`
- `humidity`
- `output_status`
- `child_lock`
- `switch`
- `switch_state`
- `work_state`
- `battery`
- `fault`

Observed raw status codes on the representative thermostat:

- `switch`
- `mode`
- `work_state`
- `temp_set`
- `upper_temp`
- `temp_current`
- `lower_temp`
- `child_lock`
- `battery_percentage`
- `fault`
- `MeasuredValue`
- `humidity_value`
- `SetpointChangeSource`
- `manual_mode_fast`
- `at_home_setting`
- `leaving_home_setting`
- `pause_setting`
- `holiday_setting`
- `switch_state`
- `floor_sensor`
- `temp_mode`
- `output_status`
- `system_status_water`

## Value Formats Observed

- setpoints such as `temp_set`, `manual_mode_fast`, `at_home_setting`, `leaving_home_setting`,
  `pause_setting`, `holiday_setting`, `upper_temp`, `lower_temp` use integer tenths of a degree
  Celsius
  - example: `210` means `21.0 C`
- `ext_measured_rs` used integer hundredths of a degree Celsius in live testing
  - example: `2100` means `21.00 C`
  - observed disabled/sentinel baseline on the Ally thermostat: `-8000`
- `mode` used string values such as `manual` and `at_home`
- some mode transitions required `last_click_time` together with `mode`, matching the existing
  implementation in the library

## Capability Matrix

### Danfoss Ally Radiator Thermostat

| Command code | Classification | Observed behavior | Notes |
| --- | --- | --- | --- |
| `switch` | confirmed writable | Toggled `true -> false` and rollback restored `true` | Boolean command accepted and reflected in readback |
| `mode` | confirmed writable | `manual -> at_home`, rollback restored `manual` | Required `last_click_time` when switching to `at_home` |
| `work_state` | rejected | `400 Bad Request` when probed with alternate enum | Strongly indicates read-only status |
| `temp_set` | confirmed writable | Repeat pass changed `210 -> 215` and rollback restored `210` | First pass timed out once, second pass was clean |
| `upper_temp` | confirmed writable | Changed `350 -> 345` and rollback restored `350` | Uses tenth-degree scale |
| `temp_current` | rejected | `400 Bad Request` when probed with alternate value | Read-only sensor/telemetry value |
| `window_state` | rejected | `400 Bad Request` when probed with `open` | Distinct from `window_toggle` |
| `lower_temp` | confirmed writable | Changed `50 -> 55` and rollback restored `50` | Uses tenth-degree scale |
| `child_lock` | confirmed writable | Toggled and rollback restored baseline | Boolean command accepted and reflected in readback |
| `battery_percentage` | rejected | `400 Bad Request` when probed with numeric change | Read-only battery telemetry |
| `fault` | rejected | `400 Bad Request` when probed with numeric change | Read-only fault/status code |
| `SetpointChangeSource` | rejected | `400 Bad Request` when probed with alternate enum | Read-only metadata |
| `manual_mode_fast` | confirmed writable | Changed `210 -> 215` and rollback restored `210` | Stable in readback |
| `window_toggle` | confirmed writable | Toggled and rollback restored baseline | Distinct from `window_state` |
| `window_state_info` | api-accepted/no-proof | API accepted the command, but no reliable effect was observed in readback | Needs extra targeted follow-up if exposed publicly |
| `at_home_setting` | confirmed writable | Changed `210 -> 215` and rollback restored `210` | Stable in readback |
| `leaving_home_setting` | confirmed writable | Changed `170 -> 175` and rollback restored `170` | Stable in readback |
| `pause_setting` | confirmed writable | Changed `50 -> 55` and rollback restored `50` | Stable in readback |
| `holiday_setting` | confirmed writable | Changed `150 -> 155` and rollback restored `150` | Stable in readback |
| `switch_state` | confirmed writable | Toggled and rollback restored baseline | Boolean command accepted and reflected in readback |
| `mounting_mode_active` | rejected | `400 Bad Request` when probed as boolean | Read-only status flag on this model |
| `ext_measured_rs` | confirmed writable | Changed `-8000 -> 2100` and rollback restored `-8000` | Strong evidence that external temperature write path is valid |
| `radiator_covered` | confirmed writable | Toggled and rollback restored baseline | Boolean command accepted and reflected in readback |
| `heat_available` | confirmed writable | Toggled and rollback restored baseline | Boolean command accepted and reflected in readback |
| `load_balance_enable` | confirmed writable | Toggled and rollback restored baseline | Boolean command accepted and reflected in readback |
| `pi_heating_demand` | rejected | `400 Bad Request` when probed with numeric change | Read-only demand/telemetry value |
| `OccupiedSetpoint` | confirmed writable | Changed `2100 -> 2150` and rollback restored `2100` | Uses hundredth-degree scale on this model |

### Danfoss Icon2 RT

| Command code | Classification | Observed behavior | Notes |
| --- | --- | --- | --- |
| `switch` | confirmed writable | Toggled and rollback restored baseline | Boolean command accepted and reflected in readback |
| `mode` | confirmed writable | `at_home -> manual`, rollback restored `at_home` | `at_home` rollback used `last_click_time` |
| `work_state` | rejected | `400 Bad Request` when probed with alternate enum | Strongly indicates read-only status |
| `temp_set` | confirmed writable | Changed `220 -> 225` and rollback restored `220` | Stable in both passes |
| `upper_temp` | confirmed writable | Changed `300 -> 295` and rollback restored `300` | Uses tenth-degree scale |
| `temp_current` | rejected | `400 Bad Request` when probed with numeric change | Read-only sensor/telemetry value |
| `lower_temp` | confirmed writable | Changed `150 -> 155` and rollback restored `150` | Uses tenth-degree scale |
| `child_lock` | confirmed writable | Toggled and rollback restored baseline | Boolean command accepted and reflected in readback |
| `battery_percentage` | rejected | `400 Bad Request` when probed with numeric change | Read-only battery telemetry |
| `fault` | rejected | `400 Bad Request` when probed with numeric change | Read-only fault/status code |
| `MeasuredValue` | rejected | `400 Bad Request` when probed with numeric change | Read-only measured floor/room temperature source |
| `humidity_value` | rejected | `400 Bad Request` when probed with numeric change | Read-only humidity telemetry |
| `SetpointChangeSource` | rejected | `400 Bad Request` when probed with alternate enum | Read-only metadata |
| `manual_mode_fast` | confirmed writable | Repeat pass changed `220 -> 225` and rollback restored `220` | Stable after rerun |
| `at_home_setting` | confirmed writable | Changed `220 -> 225` and rollback restored `220` | Stable in readback |
| `leaving_home_setting` | confirmed writable | Changed `180 -> 185` and rollback restored `180` | Stable in readback |
| `pause_setting` | confirmed writable | Changed `150 -> 155` and rollback restored `150` | Stable in readback |
| `holiday_setting` | confirmed writable | Changed `150 -> 155` and rollback restored `150` | Stable in readback |
| `switch_state` | confirmed writable | Toggled and rollback restored baseline | Boolean command accepted and reflected in readback |
| `floor_sensor` | rejected | `400 Bad Request` when probed as boolean | Read-only configuration/status on this model |
| `temp_mode` | rejected | `400 Bad Request` when probed with alternate enum | Read-only operating description on this model |
| `output_status` | rejected | `400 Bad Request` when probed with alternate enum | Read-only state/telemetry on this model |
| `system_status_water` | rejected | `400 Bad Request` when probed with alternate enum | Read-only system status on this model |

## Practical Conclusions For The Library

- `manual_mode_fast`, `at_home_setting`, `leaving_home_setting`, `pause_setting`, `holiday_setting`,
  `temp_set`, and `mode` are backed by live evidence on the tested device types.
- `ext_measured_rs` is backed by live evidence on the Ally radiator thermostat and is a valid basis
  for `set_external_temperature()`.
- `upper_temp` and `lower_temp` are writable on both tested thermostat models, but there is not yet
  matching helper support in the library.
- `switch`, `switch_state`, and `child_lock` are writable on both tested thermostat models.
- `window_toggle`, `radiator_covered`, `heat_available`, `load_balance_enable`, and
  `OccupiedSetpoint` were writable on the tested Ally radiator thermostat.
- several sensor/telemetry/status properties consistently returned `400 Bad Request` and should be
  treated as read-only until proven otherwise.
- `temp_set` should not be considered unreliable solely because of the earlier timeout on the Ally
  thermostat; the full matrix pass and repeat pass both confirmed normal behavior.
- `GET /devices/{device_id}` is currently the safest baseline/readback endpoint for write research.

## Suggested Next Code Changes

- keep `set_external_temperature()` mapped to `ext_measured_rs`
- keep generic `send_command()` as the fallback path
- optionally add explicit helpers for `upper_temp`, `lower_temp`, `child_lock`, and
  other now-confirmed writable commands if these are useful to consumers
- keep `window_state_info` out of dedicated helpers until its semantics are clearer, since the API
  accepted the command but readback did not provide strong proof of effect
