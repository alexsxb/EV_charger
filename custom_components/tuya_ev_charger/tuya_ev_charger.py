from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import tinytuya  # type: ignore

from .const import (
    ALLOWED_CURRENTS,
    CHARGER_PROFILE_CUSTOM_JSON,
    CHARGER_PROFILE_DEPOW_V2,
    CHARGER_PROFILE_GENERIC_V1,
    CHARGER_PROFILES,
    DEFAULT_CHARGER_PROFILE,
    DEFAULT_CHARGER_PROFILE_JSON,
    DP_ADJUST_CURRENT,
    DP_ALARM,
    DP_CHARGE_HISTORY,
    DP_CHARGER_INFO,
    DP_CURRENT_TARGET,
    DP_DO_CHARGE,
    DP_DOWNCOUNTER,
    DP_MAX_CURRENT_CFG,
    DP_METRICS,
    DP_NFC_CFG,
    DP_NUM,
    DP_PRODUCT_VARIANT,
    DP_REBOOT,
    DP_SCHEDULE,
    DP_SELFTEST,
    DP_WORK_STATE,
    DP_WORK_STATE_DEBUG,
)

LOGGER = logging.getLogger(__name__)
# The charger's relay/status update can lag a plain re-read by a few seconds.
# 3x0.5s (1.5s total) was too tight and produced false "not reflected" errors
# even though the charger did apply the change a moment later. 8x1.0s (8s
# total) covers every DP, do_charge (140) included: the earlier 30 s window
# for DP 140 was working around poll/command races and bare ACKs read as
# rejections, both fixed in _async_send_command.
COMMAND_VERIFY_RETRIES = 8
COMMAND_VERIFY_DELAY_S = 1.0


@dataclass(slots=True, frozen=True)
class DPProfile:
    metrics: str
    charger_info: str
    work_state: str
    work_state_debug: str
    do_charge: str
    current_target: str
    max_current_cfg: str
    nfc_cfg: str
    downcounter: str
    selftest: str
    alarm: str
    charge_history: str
    adjust_current: str
    product_variant: str
    dp_num: str
    reboot: str


DP_PROFILE_MAP: dict[str, DPProfile] = {
    CHARGER_PROFILE_DEPOW_V2: DPProfile(
        metrics=DP_METRICS,
        charger_info=DP_CHARGER_INFO,
        work_state=DP_WORK_STATE,
        work_state_debug=DP_WORK_STATE_DEBUG,
        do_charge=DP_DO_CHARGE,
        current_target=DP_CURRENT_TARGET,
        max_current_cfg=DP_MAX_CURRENT_CFG,
        nfc_cfg=DP_NFC_CFG,
        downcounter=DP_DOWNCOUNTER,
        selftest=DP_SELFTEST,
        alarm=DP_ALARM,
        charge_history=DP_CHARGE_HISTORY,
        adjust_current=DP_ADJUST_CURRENT,
        product_variant=DP_PRODUCT_VARIANT,
        dp_num=DP_NUM,
        reboot=DP_REBOOT,
    ),
    # Generic profile currently mirrors depow_v2 mappings and is meant as
    # an extension point for additional charger firmwares.
    CHARGER_PROFILE_GENERIC_V1: DPProfile(
        metrics=DP_METRICS,
        charger_info=DP_CHARGER_INFO,
        work_state=DP_WORK_STATE,
        work_state_debug=DP_WORK_STATE_DEBUG,
        do_charge=DP_DO_CHARGE,
        current_target=DP_CURRENT_TARGET,
        max_current_cfg=DP_MAX_CURRENT_CFG,
        nfc_cfg=DP_NFC_CFG,
        downcounter=DP_DOWNCOUNTER,
        selftest=DP_SELFTEST,
        alarm=DP_ALARM,
        charge_history=DP_CHARGE_HISTORY,
        adjust_current=DP_ADJUST_CURRENT,
        product_variant=DP_PRODUCT_VARIANT,
        dp_num=DP_NUM,
        reboot=DP_REBOOT,
    ),
}

# Friendly status enum, taken from the equivalent tuya_local device config for
# this same product - maps the raw x_work_state_debug (109) string to stable,
# translatable values. "plugged_in" (IDLEINS) is the one that was previously
# falling through to a bare "UNKNOWN"/"idle"-looking state.
STATUS_MAP: dict[str, str] = {
    "SLEEP": "sleep",
    "IDLE": "idle",
    "IDLEINS": "plugged_in",
    "WORKING": "charging",
    "WAIT": "waiting",
    "ERRORPAUSE": "fault",
    "PAUSE": "paused",
    "STOP": "charged",
}
STATUS_OPTIONS: tuple[str, ...] = tuple(STATUS_MAP.values())


@dataclass(slots=True, frozen=True)
class EVMetrics:
    voltage_l1: float
    current_l1: float
    power_l1: float
    temperature: float
    work_state: int | None
    work_state_debug: str
    status: str | None
    do_charge: bool | None
    current_target: int | None
    max_current_cfg: int | None
    nfc_enabled: bool | None
    downcounter: int | None
    selftest: str | None
    alarm: str | None
    adjust_current_options: tuple[int, ...] | None
    product_variant: int | None
    charger_info: dict[str, Any]
    schedule_enabled: bool
    schedule_start: str | None
    schedule_end: str | None
    session_duration_s: int | None
    session_energy_kwh: float | None
    last_session_energy_kwh: float | None
    last_session_duration_s: int | None


class TuyaEVChargerClient:
    def __init__(
        self,
        device_id: str,
        host: str,
        local_key: str,
        protocol_version: str,
        charger_profile: str = DEFAULT_CHARGER_PROFILE,
        charger_profile_json: str = DEFAULT_CHARGER_PROFILE_JSON,
    ) -> None:
        self._device_id = device_id
        self._host = host
        self._local_key = local_key
        self._protocol_version = protocol_version
        self._dp_profile, self._dp = _resolve_profile(
            charger_profile,
            charger_profile_json,
        )
        self._device: tinytuya.Device | None = None
        # The charger accepts a single local connection and tinytuya's Device is
        # not thread-safe. A command runs on one worker thread while the
        # coordinator's poll runs on another, both on this one Device object, so
        # every access to `self._device` is serialised here. Without it a write
        # that lands mid-poll corrupts the socket and tinytuya returns None,
        # which used to read as "Command rejected for DP 140".
        self._io_lock = asyncio.Lock()

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def host(self) -> str:
        return self._host

    @property
    def dp_profile(self) -> str:
        return self._dp_profile

    async def async_connect(self) -> None:
        async with self._io_lock:
            self._device = tinytuya.Device(
                dev_id=self._device_id,
                address=self._host,
                local_key=self._local_key,
                version=self._protocol_version,
            )
            self._device.set_socketTimeout(5)

    async def async_update_host(self, host: str) -> None:
        """Point the client at a new IP (after a DHCP change) and reconnect."""
        self._host = host
        await self.async_connect()

    async def async_probe_host(self, host: str) -> bool:
        """Return True if our charger answers at ``host``.

        Opens a throwaway connection with our own device_id/local_key and reads
        the live status (grid voltage & co). Only the real charger decrypts the
        reply with our local_key, so a successful read confirms identity without
        relying on the MAC or the advertised device_id. The live client is left
        untouched until the caller decides to adopt the new host.
        """

        def _probe() -> bool:
            try:
                device = tinytuya.Device(
                    dev_id=self._device_id,
                    address=host,
                    local_key=self._local_key,
                    version=self._protocol_version,
                )
                device.set_socketTimeout(5)
                payload: Any = device.status()
            except Exception:  # noqa: BLE001 - probing is best-effort
                return False
            return (
                isinstance(payload, dict)
                and "Error" not in payload
                and isinstance(payload.get("dps"), dict)
                and bool(payload["dps"])
            )

        return await asyncio.to_thread(_probe)

    async def async_set_charge_current(self, amperage: int) -> bool:
        if amperage < min(ALLOWED_CURRENTS) or amperage > max(ALLOWED_CURRENTS):
            raise ValueError(
                f"Current setpoint {amperage}A is out of supported range "
                f"({min(ALLOWED_CURRENTS)}-{max(ALLOWED_CURRENTS)}A)."
            )
        return await self._async_send_command(self._dp.current_target, amperage)

    async def async_set_charge_enabled(self, enabled: bool) -> bool:
        return await self._async_send_command(self._dp.do_charge, enabled)

    async def async_set_nfc_enabled(self, enabled: bool) -> bool:
        return await self._async_send_command(self._dp.nfc_cfg, enabled)

    async def async_reboot(self) -> bool:
        # Depending on firmware variants, reboot may accept bool, int, or string payloads.
        for payload in (True, 1, "1"):
            if await self._async_send_command(self._dp.reboot, payload, verify=False):
                return True
        return False

    async def async_get_metrics(self) -> EVMetrics | None:
        async with self._io_lock:
            dps = await self._async_get_dps_payload()
        if dps is None:
            return None

        metrics_dict = _parse_json_object(dps.get(self._dp.metrics, "{}"))
        charger_info = _parse_json_object(dps.get(self._dp.charger_info, "{}"))
        schedule_dict = _parse_json_object(dps.get(DP_SCHEDULE, "{}"))
        # DP 105 (x_charge_history) is a frozen record of the last *completed*
        # session - unlike x_metrics' "e"/"d", it survives after the session
        # ends and the live values reset to 0. Field names ("c" for energy,
        # "d" for duration in plain seconds) are adopted from an upstream fork
        # working on the same product; not yet cross-checked against a real
        # completed session on this specific device.
        history_dict = _parse_json_object(dps.get(self._dp.charge_history, "{}"))

        work_state_debug = _coerce_optional_text(dps.get(self._dp.work_state_debug)) or "UNKNOWN"
        work_state_debug = work_state_debug.strip().upper()

        do_charge = _coerce_optional_bool(dps.get(self._dp.do_charge))

        # The charger keeps reporting the last power/current reading even after
        # a session ends (idle/paused/plugged-in-not-charging), which makes the
        # power sensor look "stuck" instead of dropping to 0. Only trust L1
        # current/power while actually charging. Voltage stays live: the AC line
        # is still there. A model that reports DP 140 counts as charging when it
        # says so, even if its DP 109 string is not one we map.
        charging = work_state_debug == "WORKING" or do_charge is True
        l1_data = metrics_dict.get("L1", [0, 0, 0])
        if not isinstance(l1_data, list) or len(l1_data) < 3:
            l1_data = [0, 0, 0]

        return EVMetrics(
            voltage_l1=_coerce_float(l1_data[0]) / 10.0,
            current_l1=_coerce_float(l1_data[1]) / 10.0 if charging else 0.0,
            power_l1=_coerce_float(l1_data[2]) / 10.0 if charging else 0.0,
            temperature=_coerce_float(metrics_dict.get("t", 0)) / 10.0,
            work_state=_coerce_optional_int(dps.get(self._dp.work_state)),
            work_state_debug=work_state_debug,
            status=STATUS_MAP.get(work_state_debug),
            do_charge=do_charge,
            current_target=_coerce_optional_int(dps.get(self._dp.current_target)),
            max_current_cfg=_coerce_optional_int(dps.get(self._dp.max_current_cfg)),
            nfc_enabled=_coerce_optional_bool(dps.get(self._dp.nfc_cfg)),
            downcounter=_coerce_optional_int(dps.get(self._dp.downcounter)),
            selftest=_coerce_optional_text(dps.get(self._dp.selftest)),
            alarm=_coerce_optional_json_text(dps.get(self._dp.alarm)),
            adjust_current_options=_parse_int_list(dps.get(self._dp.adjust_current)),
            product_variant=_coerce_optional_int(dps.get(self._dp.product_variant)),
            charger_info=charger_info,
            schedule_enabled=schedule_dict.get("m", 0) == 2,
            schedule_start=_coerce_optional_text(schedule_dict.get("ss")),
            schedule_end=_coerce_optional_text(schedule_dict.get("se")),
            # "d" and "e" are undocumented sub-fields of x_metrics observed on the
            # depow_v2 firmware: session duration and session energy.
            # "d" is scaled x10 just like "t" (530 shown for a real 53s charge).
            # "e" is scaled x10 too, confirmed against a live device dump: e=9
            # with the app showing 0.9 kWh (an earlier /100 guess was wrong).
            session_duration_s=_scale_optional_int(metrics_dict.get("d"), 10.0),
            session_energy_kwh=_scale_optional_float(metrics_dict.get("e"), 10.0),
            last_session_energy_kwh=_scale_optional_float(history_dict.get("c"), 10.0),
            last_session_duration_s=_coerce_optional_int(history_dict.get("d")),
        )

    async def async_set_schedule(self, enabled: bool, start: str, end: str) -> bool:
        payload = json.dumps(
            {"m": 2 if enabled else 0, "dt": 0, "ss": start, "se": end},
            separators=(",", ":"),
        )
        return await self._async_send_command(DP_SCHEDULE, payload, verify=False)

    async def async_get_raw_dps(self) -> dict[str, Any] | None:
        async with self._io_lock:
            return await self._async_get_dps_payload()

    async def _async_send_command(
        self,
        dp_id: str,
        value: Any,
        verify: bool = True,
        retries: int = COMMAND_VERIFY_RETRIES,
        delay_s: float = COMMAND_VERIFY_DELAY_S,
    ) -> bool:
        """Write a DP and confirm it took, holding the charger's single slot throughout.

        tinytuya's ``set_value`` returns one of three things:

        * a ``dict`` **with** an ``"Error"`` key -- a genuine transport failure
          (offline, timeout, undecryptable).
        * ``None`` -- the charger sent a bare ACK and no ``dps`` echo. This is
          the *normal* reply to a write-only DP on protocol 3.4/3.5 (DP 140 in
          particular), not a rejection.
        * a ``dict`` without ``"Error"`` -- accepted with an echo.

        Only the first is a failure. The other two fall through to read-back
        verification, which tolerates a charger that never echoes the DP.
        """
        async with self._io_lock:
            device = self._get_device()
            response: Any = await asyncio.to_thread(device.set_value, dp_id, value)

            if isinstance(response, dict) and "Error" in response:
                LOGGER.warning("Command to DP %s failed: %s", dp_id, response["Error"])
                return False

            if response is None:
                LOGGER.debug(
                    "DP %s: charger acknowledged without echoing it back; verifying by read-back.",
                    dp_id,
                )

            if not verify:
                return True

            verdict = await self._async_verify_command(
                dp_id, value, retries=retries, delay_s=delay_s
            )
            if verdict is not False:
                # True (echoed match) or None (this charger never reports the DP).
                return True

        LOGGER.warning("Command accepted but not reflected in status for DP %s.", dp_id)
        return False

    async def _async_verify_command(
        self,
        dp_id: str,
        expected: Any,
        retries: int = COMMAND_VERIFY_RETRIES,
        delay_s: float = COMMAND_VERIFY_DELAY_S,
    ) -> bool | None:
        """Check the charger echoes back a written DP.

        Returns True on a match, False on a genuine mismatch, and None when the
        DP is simply absent from the status payload. Some models never report
        write-only DPs (e.g. DP 140), so demanding an echo there would fail
        every command even though the charger obeyed it.

        The full retry budget is kept for chargers that *do* report the DP but
        echo it late; a DP missing from two clean reads is taken as never
        reported, so the caller is not made to wait out all the retries.

        Must be called with ``self._io_lock`` held.
        """
        saw_dp = False
        reads_without_dp = 0
        for _ in range(retries):
            await asyncio.sleep(delay_s)
            dps = await self._async_get_dps_payload()
            if dps is None:
                continue
            if dp_id not in dps:
                reads_without_dp += 1
                if reads_without_dp >= 2:
                    break
                continue
            saw_dp = True
            if _values_match(dps.get(dp_id), expected):
                return True

        if not saw_dp:
            LOGGER.debug(
                "DP %s is not reported by this charger; assuming the command was applied.",
                dp_id,
            )
            return None
        return False

    async def _async_get_dps_payload(self) -> dict[str, Any] | None:
        """Read the charger's DPS. Must be called with ``self._io_lock`` held."""
        device = self._get_device()
        payload: Any = await asyncio.to_thread(device.status)

        if not isinstance(payload, dict):
            LOGGER.error("Invalid status payload type: %s", type(payload).__name__)
            return None

        if "Error" in payload:
            LOGGER.error("Charger returned an error payload: %s", payload["Error"])
            return None

        dps: Any = payload.get("dps", {})
        if not isinstance(dps, dict):
            LOGGER.error("Missing or invalid DPS payload.")
            return None
        return dps

    def _get_device(self) -> tinytuya.Device:
        if self._device is None:
            raise RuntimeError("Device client is not initialized. Call async_connect first.")
        return self._device


def _resolve_profile(profile: str, custom_json: str) -> tuple[str, DPProfile]:
    normalized = str(profile).strip().lower()
    if normalized == CHARGER_PROFILE_CUSTOM_JSON:
        custom_profile = _parse_custom_dp_profile(custom_json)
        if custom_profile is not None:
            return CHARGER_PROFILE_CUSTOM_JSON, custom_profile
        LOGGER.warning(
            "Invalid custom charger profile JSON mapping, falling back to '%s'.",
            DEFAULT_CHARGER_PROFILE,
        )
        return DEFAULT_CHARGER_PROFILE, DP_PROFILE_MAP[DEFAULT_CHARGER_PROFILE]
    if normalized in CHARGER_PROFILES and normalized in DP_PROFILE_MAP:
        return normalized, DP_PROFILE_MAP[normalized]
    return DEFAULT_CHARGER_PROFILE, DP_PROFILE_MAP[DEFAULT_CHARGER_PROFILE]


def _parse_custom_dp_profile(raw_json: str) -> DPProfile | None:
    text = str(raw_json).strip()
    if not text:
        return None
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError:
        LOGGER.debug("Unable to decode custom charger profile JSON.")
        return None
    if not isinstance(payload, dict):
        return None

    base_profile = DP_PROFILE_MAP[DEFAULT_CHARGER_PROFILE]
    values: dict[str, str] = {}
    for field_name in DPProfile.__dataclass_fields__:
        raw_value = payload.get(field_name, getattr(base_profile, field_name))
        if raw_value is None:
            return None
        text_value = str(raw_value).strip()
        if not text_value:
            return None
        values[field_name] = text_value
    return DPProfile(**values)


def _parse_json_object(raw_value: Any) -> dict[str, Any]:
    if isinstance(raw_value, dict):
        return raw_value
    if not isinstance(raw_value, str):
        return {}

    try:
        decoded: Any = json.loads(raw_value)
    except json.JSONDecodeError:
        LOGGER.debug("Unable to decode JSON object: %s", raw_value)
        return {}

    if isinstance(decoded, dict):
        return decoded
    return {}


def _parse_int_list(raw_value: Any) -> tuple[int, ...] | None:
    parsed_list: list[Any]
    if isinstance(raw_value, list):
        parsed_list = raw_value
    elif isinstance(raw_value, str):
        try:
            decoded: Any = json.loads(raw_value)
        except json.JSONDecodeError:
            return None
        if not isinstance(decoded, list):
            return None
        parsed_list = decoded
    else:
        return None

    cleaned: list[int] = []
    for item in parsed_list:
        value = _coerce_optional_int(item)
        if value is None:
            continue
        cleaned.append(value)
    if not cleaned:
        return None
    return tuple(sorted(set(cleaned)))


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _coerce_optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scale_optional_float(value: Any, divisor: float) -> float | None:
    raw = _coerce_optional_float(value)
    if raw is None:
        return None
    return raw / divisor


def _scale_optional_int(value: Any, divisor: float) -> int | None:
    raw = _coerce_optional_float(value)
    if raw is None:
        return None
    return round(raw / divisor)


def _coerce_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
    else:
        text = str(value).strip()
    if not text:
        return None
    return text


def _coerce_optional_json_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    try:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    except TypeError:
        return _coerce_optional_text(value)


def _coerce_optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "on"}:
            return True
        if lowered in {"false", "0", "off"}:
            return False
    return None


def _values_match(received: Any, expected: Any) -> bool:
    # Compare on the type actually written. `expected` is a real bool for the
    # on/off DPs and an int for the numeric ones (e.g. DP 150 current) --
    # coercing an int like 16 through bool() first made it "match" a read-back
    # of 10, so a write that never took looked verified.
    if isinstance(expected, bool):
        received_bool = _coerce_optional_bool(received)
        return received_bool is not None and received_bool == expected

    expected_int = _coerce_optional_int(expected)
    if expected_int is not None:
        received_int = _coerce_optional_int(received)
        return received_int is not None and received_int == expected_int

    if isinstance(expected, str):
        return str(received).strip() == expected.strip()
    return received == expected
