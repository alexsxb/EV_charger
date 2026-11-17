from __future__ import annotations

import logging
from time import monotonic
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICE_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    REDISCOVERY_COOLDOWN_SECONDS,
    REDISCOVERY_SCAN_SECONDS,
)
from .discovery import async_scan_devices_by_id
from .tuya_ev_charger import PILOT_NO_VEHICLE_MIN_V, EVMetrics, TuyaEVChargerClient

LOGGER = logging.getLogger(__name__)

# While the auto-start lock is on, poll at least this often so a charge that
# slips through anyway is caught (and stopped) within seconds.
AUTOSTART_LOCK_POLL_INTERVAL = timedelta(seconds=10)
# The lock parks the charger in scheduled mode with a one-minute window that
# never comes round: placed this far ahead, and moved again once it gets closer
# than the margin. The wide margin also covers a charger clock that does not
# match Home Assistant's (time zone, DST).
DEAD_WINDOW_AHEAD = timedelta(hours=12)
DEAD_WINDOW_MARGIN = timedelta(hours=6)
# Every DP write makes the charger beep, even when nothing changes. Writes the
# lock makes on its own (not on a user action) are spaced at least this far
# apart, so a read-back it does not understand can never turn into a loop.
AUTO_WRITE_MIN_INTERVAL_S = 15 * 60
# Only after this many polls in a row without a vehicle is the car taken as
# unplugged (which ends a requested session), so a state the charger passes
# through briefly cannot end it.
UNPLUGGED_CONFIRM_POLLS = 3


class TuyaEVChargerDataUpdateCoordinator(DataUpdateCoordinator[EVMetrics]):
    def __init__(
        self,
        hass: HomeAssistant,
        client: TuyaEVChargerClient,
        entry: ConfigEntry,
        update_interval: timedelta = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self.client = client
        self.entry = entry
        self.last_discovery: dict | None = None
        self._last_rediscovery_at: float = 0.0
        self._base_update_interval = update_interval
        # Auto-start lock: the charger ignores the DPs meant to keep it idle on
        # plug-in, but it does wait for its schedule window - and only charges
        # on request once scheduled mode is switched off.
        self.autostart_locked = False
        self._charge_requested = False
        # Start of the dead window the lock last wrote (local time), or None
        # when it has not parked the charger since startup / being switched on.
        self._parked_start: datetime | None = None
        # Whether the current parked window was already re-written once because
        # the read-back did not match it; after that the lock trusts its own
        # record, so a firmware reporting DP 151 differently cannot keep it
        # writing (and the charger beeping).
        self._parked_rechecked = False
        self._last_auto_write = float("-inf")
        self._unplugged_polls = 0
        self._last_work_state = "UNKNOWN"

    def restore_autostart_lock(self, locked: bool) -> None:
        """Set the lock from the restored switch state, without touching the charger."""
        self.autostart_locked = locked
        self._apply_poll_interval()
        # The schedule entities' availability follows the lock.
        self.async_update_listeners()

    async def async_set_autostart_locked(self, locked: bool) -> None:
        self.autostart_locked = locked
        self._apply_poll_interval()
        self.async_update_listeners()
        if locked:
            if not self._charge_requested:
                await self._async_arm_dead_window()
        else:
            # Back to the charger's default: charge as soon as a car is plugged in.
            self._parked_start = None
            await self._async_set_schedule_enabled(False)

    def _apply_poll_interval(self) -> None:
        self.update_interval = (
            min(self._base_update_interval, AUTOSTART_LOCK_POLL_INTERVAL)
            if self.autostart_locked
            else self._base_update_interval
        )

    async def async_set_charging(self, enabled: bool) -> bool:
        """Start or stop a session on purpose (switch.charge_session).

        With the lock on, the charger only charges once scheduled mode is off,
        so a start releases the schedule and a stop parks it again.
        """
        # Before the commands, so a poll landing in between does not take this
        # start for one the car made by itself.
        self._charge_requested = enabled
        if enabled and self.autostart_locked:
            if not await self._async_set_schedule_enabled(False):
                return False
            self._parked_start = None
        ok = await self.client.async_set_charge_enabled(enabled)
        if not enabled and self.autostart_locked:
            ok = await self._async_arm_dead_window() or ok
        return ok

    async def _async_set_schedule_enabled(self, enabled: bool) -> bool:
        data = self.data
        start = (data.schedule_start if data else None) or "00:00"
        end = (data.schedule_end if data else None) or "00:00"
        return await self.client.async_set_schedule(enabled, start, end)

    async def _async_arm_dead_window(self) -> bool:
        start = dt_util.now() + DEAD_WINDOW_AHEAD
        end = start + timedelta(minutes=1)
        LOGGER.debug("Auto-start lock: parking the schedule at %s.", _hhmm(start))
        self._last_auto_write = monotonic()
        if not await self.client.async_set_schedule(True, _hhmm(start), _hhmm(end)):
            return False
        self._parked_start = start
        self._parked_rechecked = False
        return True

    async def _async_update_data(self) -> EVMetrics:
        metrics = await self._async_poll()
        if metrics is not None:
            return await self._async_enforce_autostart_lock(metrics)
        raise UpdateFailed(
            f"Charger unreachable at {self.client.host} (no telemetry received)."
        )

    async def _async_enforce_autostart_lock(self, metrics: EVMetrics) -> EVMetrics:
        """Keep the charger parked in scheduled mode while the lock is on."""
        # DP 109 is not in every status reply either: go by the last state the
        # charger did report.
        if metrics.work_state_debug != "UNKNOWN":
            self._last_work_state = metrics.work_state_debug
        state = self._last_work_state
        self._unplugged_polls = self._unplugged_polls + 1 if _no_vehicle(metrics, state) else 0
        if self._unplugged_polls >= UNPLUGGED_CONFIRM_POLLS:
            self._charge_requested = False
        if not self.autostart_locked:
            return metrics

        # DP 151 is not in every status reply; without it the schedule is
        # unknown, not "off".
        schedule_known = metrics.schedule_start is not None
        charging = state == "WORKING" or metrics.do_charge is True
        if charging and schedule_known and not metrics.schedule_enabled:
            # Only a released schedule lets it charge, so this one was asked
            # for - including one running across a Home Assistant restart.
            self._charge_requested = True
        if self._charge_requested:
            return metrics
        if monotonic() - self._last_auto_write < AUTO_WRITE_MIN_INTERVAL_S:
            return metrics

        if charging and schedule_known and metrics.schedule_enabled:
            # Parked but charging anyway (e.g. the window came round while Home
            # Assistant was down): stop it and park again.
            LOGGER.info("Charge started while the auto-start lock is on; stopping it.")
            await self.client.async_set_charge_enabled(False)
            await self._async_arm_dead_window()
        elif reason := self._parking_reason(metrics, schedule_known):
            await self._async_arm_dead_window()
            if reason == "mismatch":
                # The re-written window keeps its one retry used.
                self._parked_rechecked = True
        else:
            return metrics
        return await self._async_fetch_metrics() or metrics

    def _parking_reason(self, metrics: EVMetrics, schedule_known: bool) -> str | None:
        """Why the charger needs (re-)parking now, or None."""
        now = dt_util.now()
        if self._parked_start is None:
            if not schedule_known:
                return None
            # After a restart: adopt a dead window already on the charger.
            start = _parked_window_start(metrics, now)
            if start is not None and start - now >= DEAD_WINDOW_MARGIN:
                self._parked_start = start
                return None
            return "not parked"
        if self._parked_start - now < DEAD_WINDOW_MARGIN:
            return "window close"
        if (
            schedule_known
            and not self._parked_rechecked
            and _parked_window_start(metrics, now) is None
        ):
            self._parked_rechecked = True
            LOGGER.warning(
                "Auto-start lock: the charger reports schedule %s/%s-%s instead of the "
                "parked window %s; parking again.",
                "on" if metrics.schedule_enabled else "off",
                metrics.schedule_start,
                metrics.schedule_end,
                _hhmm(self._parked_start),
            )
            return "mismatch"
        return None

    async def _async_poll(self) -> EVMetrics | None:
        metrics = await self._async_fetch_metrics()
        if metrics is not None:
            return metrics

        # A session the charger has wedged (same IP, same key) only clears with
        # a fresh client - a reload used to be the only cure. Rebuild once and
        # retry before assuming the charger moved.
        LOGGER.debug("Charger poll failed; rebuilding the connection and retrying once.")
        await self.client.async_connect()
        metrics = await self._async_fetch_metrics()
        if metrics is not None:
            return metrics

        # Communication failed: after a power cycle the charger's DHCP IP may
        # have changed. Relocate it (scan + live read) and retry once before failing.
        if await self._async_try_rediscover_host():
            return await self._async_fetch_metrics()
        return None

    async def _async_fetch_metrics(self) -> EVMetrics | None:
        try:
            return await self.client.async_get_metrics()
        except Exception as err:  # noqa: BLE001 - surfaced as UpdateFailed upstream
            LOGGER.debug("Charger poll failed: %s", err)
            return None

    async def _async_try_rediscover_host(self) -> bool:
        """Relocate the charger when its IP changed, confirming by a live read.

        Scans the LAN for Tuya devices, then probes candidate IPs with our own
        local_key (a real status/voltage read). The device that answers is our
        charger, whatever its advertised MAC or device_id. Candidates advertising
        our device_id are tried first. Only the in-memory client host is updated
        (immediate recovery, no config-entry reload); the new IP is persisted
        lazily on the next setup. Throttled so an offline charger does not scan
        on every poll.
        """
        now = self.hass.loop.time()
        if now - self._last_rediscovery_at < REDISCOVERY_COOLDOWN_SECONDS:
            return False
        self._last_rediscovery_at = now

        candidates = await async_scan_devices_by_id(
            self.hass, scantime=REDISCOVERY_SCAN_SECONDS
        )
        if not candidates:
            LOGGER.debug("Re-discovery scan found no Tuya devices on the network.")
            return False

        device_id = str(self.entry.data.get(CONF_DEVICE_ID, "")).strip()
        current_host = self.client.host

        for host in self._ordered_candidate_hosts(candidates, device_id, current_host):
            if not await self.client.async_probe_host(host):
                continue
            LOGGER.info(
                "Charger confirmed at %s (was %s) via live telemetry read.",
                host,
                current_host,
            )
            self.last_discovery = self._discovery_for_host(candidates, host)
            await self.client.async_update_host(host)
            return True

        LOGGER.debug(
            "Re-discovery probed %d candidate(s) but none answered our local_key.",
            len(candidates),
        )
        return False

    @staticmethod
    def _ordered_candidate_hosts(
        candidates: dict[str, dict],
        device_id: str,
        current_host: str,
    ) -> list[str]:
        """Candidate IPs to probe: our device_id's IP first, then the rest.

        The current (already-failed) host is skipped since the normal poll just
        tried it.
        """
        ordered: list[str] = []
        mine = candidates.get(device_id) if device_id else None
        if mine and mine.get("ip"):
            ordered.append(str(mine["ip"]))
        for info in candidates.values():
            host = str(info.get("ip", "")).strip()
            if host and host not in ordered:
                ordered.append(host)
        return [host for host in ordered if host and host != current_host]

    @staticmethod
    def _discovery_for_host(candidates: dict[str, dict], host: str) -> dict:
        for info in candidates.values():
            if str(info.get("ip", "")).strip() == host:
                return info
        return {"ip": host}




def _parked_window_start(metrics: EVMetrics, now: datetime) -> datetime | None:
    """Next start of the charger's schedule if it is a parked one-minute window."""
    if not metrics.schedule_enabled or not metrics.schedule_start or not metrics.schedule_end:
        return None
    try:
        start = _minutes(metrics.schedule_start)
        end = _minutes(metrics.schedule_end)
    except ValueError:
        return None
    if (end - start) % (24 * 60) != 1:
        return None
    until_start = (start - (now.hour * 60 + now.minute)) % (24 * 60)
    return now.replace(second=0, microsecond=0) + timedelta(minutes=until_start)

def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _hhmm(moment: datetime) -> str:
    return f"{moment.hour:02d}:{moment.minute:02d}"


def _no_vehicle(metrics: EVMetrics, state: str) -> bool:
    """Whether no car is connected.

    The control-pilot voltage (DP 106 "cp") says so directly. DP 109 SLEEP is
    only a fallback: this charger freezes it on SLEEP once its display goes
    dark, even with a car plugged in and charging.
    """
    if metrics.pilot_voltage is not None:
        return metrics.pilot_voltage >= PILOT_NO_VEHICLE_MIN_V
    return state == "SLEEP"
