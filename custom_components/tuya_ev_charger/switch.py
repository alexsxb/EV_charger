from __future__ import annotations

from time import monotonic

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import TuyaEVChargerRuntimeData
from .const import (
    CARD_ROLE_CHARGE_SESSION,
    CARD_ROLE_INDEX,
    CARD_ROLE_SCHEDULE_ENABLED,
)
from .entity import TuyaEVChargerEntity

# A charger without DP 140 reports only its operating state, and after a start
# command it steps PAUSE -> IDLEINS -> WORKING over several seconds. Hold the
# commanded state until the charger's own state agrees, so the switch does not
# flick back off for those seconds right after the user turns it on.
_PENDING_TIMEOUT_S = 90.0
# Operating states (raw DP 109) that mean a pending "on" will not arrive -- no
# cable, a fault, or a finished session. Stop waiting and show the real state.
_NOT_STARTING_STATES = frozenset({"SLEEP", "IDLE", "STOP", "ERRORPAUSE"})

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime_data: TuyaEVChargerRuntimeData = entry.runtime_data
    async_add_entities(
        [
            TuyaEVChargerChargeSessionSwitch(entry, runtime_data),
            TuyaEVChargerNfcSwitch(entry, runtime_data),
            TuyaEVChargerScheduleSwitch(entry, runtime_data),
            TuyaEVChargerAutostartLockSwitch(entry, runtime_data),
        ]
    )


class TuyaEVChargerChargeSessionSwitch(TuyaEVChargerEntity, SwitchEntity):
    _attr_translation_key = "charge_session"
    _attr_icon = "mdi:ev-station"

    def __init__(self, entry: ConfigEntry, runtime_data: TuyaEVChargerRuntimeData) -> None:
        super().__init__(
            entry=entry,
            runtime_data=runtime_data,
            card_role=CARD_ROLE_CHARGE_SESSION,
            card_index=CARD_ROLE_INDEX[CARD_ROLE_CHARGE_SESSION],
        )
        self._attr_unique_id = f"{runtime_data.client.device_id}_charge_session"
        self._pending_charge: bool | None = None
        self._pending_since: float = 0.0

    @property
    def _reported_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        if data.do_charge is not None:
            return data.do_charge
        return data.work_state_debug == "WORKING"

    @property
    def is_on(self) -> bool:
        """The charger's reported state, or the commanded one while it catches up."""
        reported = self._reported_on
        pending = self._pending_charge
        if pending is None:
            return bool(reported)

        data = self.coordinator.data
        stalled = bool(
            pending and data is not None and data.work_state_debug in _NOT_STARTING_STATES
        )
        if reported == pending or stalled or monotonic() - self._pending_since > _PENDING_TIMEOUT_S:
            self._pending_charge = None
            return bool(reported)
        return pending

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._async_set_charging(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._async_set_charging(False)

    async def _async_set_charging(self, enabled: bool) -> None:
        if not await self.coordinator.async_set_charging(enabled):
            raise HomeAssistantError(
                "Unable to start charging session."
                if enabled
                else "Unable to stop charging session."
            )
        self._pending_charge = enabled
        self._pending_since = monotonic()
        await self.coordinator.async_request_refresh()


class TuyaEVChargerNfcSwitch(TuyaEVChargerEntity, SwitchEntity):
    _attr_translation_key = "nfc_enabled"
    _attr_icon = "mdi:nfc"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, runtime_data: TuyaEVChargerRuntimeData) -> None:
        super().__init__(entry=entry, runtime_data=runtime_data)
        self._attr_unique_id = f"{runtime_data.client.device_id}_nfc_enabled"

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        if data is None or data.nfc_enabled is None:
            return False
        return data.nfc_enabled

    async def async_turn_on(self, **kwargs: object) -> None:
        if not await self._runtime_data.client.async_set_nfc_enabled(True):
            raise HomeAssistantError("Unable to enable NFC.")
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: object) -> None:
        if not await self._runtime_data.client.async_set_nfc_enabled(False):
            raise HomeAssistantError("Unable to disable NFC.")
        await self.coordinator.async_request_refresh()


class TuyaEVChargerScheduleSwitch(TuyaEVChargerEntity, SwitchEntity):
    _attr_translation_key = "schedule_enabled"
    _attr_icon = "mdi:clock-outline"

    def __init__(self, entry: ConfigEntry, runtime_data: TuyaEVChargerRuntimeData) -> None:
        super().__init__(
            entry=entry,
            runtime_data=runtime_data,
            card_role=CARD_ROLE_SCHEDULE_ENABLED,
            card_index=CARD_ROLE_INDEX[CARD_ROLE_SCHEDULE_ENABLED],
        )
        self._attr_unique_id = f"{runtime_data.client.device_id}_schedule_enabled"

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        return bool(data and data.schedule_enabled)

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        data = self.coordinator.data
        start = (data.schedule_start if data else None) or "00:00"
        end = (data.schedule_end if data else None) or "00:00"
        if not await self._runtime_data.client.async_set_schedule(enabled, start, end):
            raise HomeAssistantError("Unable to update charging schedule.")
        await self.coordinator.async_request_refresh()


class TuyaEVChargerAutostartLockSwitch(TuyaEVChargerEntity, SwitchEntity, RestoreEntity):
    """Keep the charger from charging on its own when a car is plugged in.

    The firmware ignores the DPs meant for this, but it waits for its schedule
    window. While on, the charger is parked in scheduled mode with a one-minute
    window that never comes round; switch.charge_session releases it for a
    session. Enforced by the charger itself, so it also holds while Home
    Assistant is down. The schedule entities are managed by the lock meanwhile.
    """

    _attr_translation_key = "autostart_lock"
    _attr_icon = "mdi:lock-clock"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, runtime_data: TuyaEVChargerRuntimeData) -> None:
        super().__init__(entry=entry, runtime_data=runtime_data)
        self._attr_unique_id = f"{runtime_data.client.device_id}_autostart_lock"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self.coordinator.restore_autostart_lock(last_state.state == "on")

    @property
    def available(self) -> bool:
        # A setting of this integration, not of the charger: always settable.
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.autostart_locked

    async def async_turn_on(self, **kwargs: object) -> None:
        await self.coordinator.async_set_autostart_locked(True)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: object) -> None:
        await self.coordinator.async_set_autostart_locked(False)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
