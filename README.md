# Tuya EV Charger Local (Home Assistant)

Local Home Assistant integration for Tuya EV chargers over LAN using `tinytuya`.

Simplified fork of [lachand/EV_charger](https://github.com/lachand/EV_charger)
by Valentin Lachand Pascal ([@lachand](https://github.com/lachand)). The solar
surplus, vehicle and charge-planning features of the original were removed;
only the charger's own data points are exposed. Selected fixes from upstream
are ported back.

Tested charger reference: `de-portable-ev-charger-3-5kw-v2`

## Quickstart

1. Add this repository in HACS (`Integrations` > `Custom repositories` > `Integration` category).
2. Install `Tuya EV Charger Local`, then restart Home Assistant.
3. Collect `host`, `device_id`, `local_key` (see section below).
4. Add the integration from `Settings` > `Devices & Services`. The setup can scan
   the LAN for the charger or take the host manually.

## Get the local_key

Recommended method (TinyTuya + Tuya IoT Cloud):

1. Create a developer account on https://iot.tuya.com.
2. Create a Smart Home cloud project.
3. Link your Tuya/Smart Life app account to that project.
4. Run:

```bash
python -m tinytuya wizard
```

5. Enter API Key, API Secret and region.
6. Read `device_id` and `local_key` from output or generated `devices.json`.

Notes:

- If you re-pair/reset the device, `local_key` can change.
- `local_key` is a secret.

## Options

- `scan_interval`
- `charger_profile` (`depow_v2`, `generic_v1` or `custom_json`)
- `charger_profile_json` (optional, custom DP mapping)

## Exposed entities

- `switch`: charging session, NFC, scheduled charging, auto-start lock
- `number`: current setpoint
- `select`: current setpoint (presets, only values the charger accepts)
- `time`: schedule start / end
- `sensor`: voltage, current and power (L1), temperature, status, work state,
  session duration / energy, last session duration / energy, plus diagnostics
  (countdown, self-test, alarm, available currents, product variant)
- `button`: reboot charger

L1 current and power read 0 whenever the charger is not charging (some
firmwares keep echoing the last value); voltage stays live.

## Auto-start lock

The charger starts charging as soon as a car is plugged in, and its firmware
ignores the settings meant to change that (DP 154 `x_socket_cfg`, DP 190
`x_plug_charge` - tested on firmware 1.9.7). With `switch.autostart_lock` on,
the integration stops any charge nobody asked for. Starts are let through when
they come from:

- `switch.charge_session` (manually or from an automation), until the car is
  unplugged
- the charger's own schedule, inside its time window

Limits: the lock only acts while Home Assistant is running, and the charger
may charge for a few seconds before it is stopped (polling runs every 10 s at
most while the lock is on). A start from the Smart Life app is stopped too.

## Device triggers

Available in the automation editor: `charge_started`, `charge_complete`,
`fault`, `plugged_in`, `unplugged_while_charging`.

## Notes

- The charger accepts a single local connection. Keep the Smart Life / Tuya app
  closed on the LAN, or polls and commands will fail intermittently.
- If the charger gets a new IP via DHCP, the integration rescans the LAN and
  follows it automatically.
