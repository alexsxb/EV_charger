from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TuyaEVChargerRuntimeData
from .entity import TuyaEVChargerEntity
from .helpers import allowed_currents
from .tuya_ev_charger import PLUG_IN_ACTION_OPTIONS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    _ = hass
    runtime_data: TuyaEVChargerRuntimeData = entry.runtime_data
    async_add_entities(
        [
            TuyaEVChargerCurrentPresetSelect(entry, runtime_data),
            TuyaEVChargerPlugInActionSelect(entry, runtime_data),
        ]
    )


class TuyaEVChargerCurrentPresetSelect(TuyaEVChargerEntity, SelectEntity):
    """Discrete preset picker for the charge current.

    Mirrors the same allowed-currents logic as the number.charge_current
    entity, but as a fixed dropdown so a UI can't offer/accept an
    unsupported value in between presets (e.g. 7A, 11A).
    """

    _attr_translation_key = "charge_current_preset"
    _attr_icon = "mdi:current-ac"

    def __init__(self, entry: ConfigEntry, runtime_data: TuyaEVChargerRuntimeData) -> None:
        super().__init__(entry=entry, runtime_data=runtime_data)
        self._attr_unique_id = f"{runtime_data.client.device_id}_charge_current_preset"

    @property
    def options(self) -> list[str]:
        return [str(value) for value in allowed_currents(self.coordinator.data)]

    @property
    def current_option(self) -> str | None:
        data = self.coordinator.data
        if data is None or data.current_target is None:
            return None
        return str(data.current_target)

    async def async_select_option(self, option: str) -> None:
        amperage = int(option)
        success = await self._runtime_data.client.async_set_charge_current(amperage)
        if not success:
            raise HomeAssistantError("Unable to update current setpoint on charger.")
        await self.coordinator.async_request_refresh()


class TuyaEVChargerPlugInActionSelect(TuyaEVChargerEntity, SelectEntity):
    """What the charger does when a cable is plugged in (DP 154).

    "idle" stops the car auto-starting a charge; start it on demand with
    switch.charge_session or the schedule instead. Unavailable on firmwares
    that do not report the DP.
    """

    _attr_translation_key = "plug_in_action"
    _attr_icon = "mdi:power-plug"
    _attr_options = list(PLUG_IN_ACTION_OPTIONS)

    def __init__(self, entry: ConfigEntry, runtime_data: TuyaEVChargerRuntimeData) -> None:
        super().__init__(entry=entry, runtime_data=runtime_data)
        self._attr_unique_id = f"{runtime_data.client.device_id}_plug_in_action"

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and data is not None and data.plug_in_action is not None

    @property
    def current_option(self) -> str | None:
        data = self.coordinator.data
        return data.plug_in_action if data is not None else None

    async def async_select_option(self, option: str) -> None:
        if not await self._runtime_data.client.async_set_plug_in_action(option):
            raise HomeAssistantError("Unable to update the plug-in action.")
        await self.coordinator.async_request_refresh()
