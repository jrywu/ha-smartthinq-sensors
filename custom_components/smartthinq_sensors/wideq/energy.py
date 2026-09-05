"""
Energy meter support based on the ThinQ "energy-history" service.

The ThinQ app "Energy Monitoring" page reads hourly energy buckets (Wh) from
`GET /v1/service/{service}/{deviceId}/energy-history`. This module turns
those buckets into a monotonic energy meter (kWh) suitable for the Home
Assistant Energy Dashboard (`TOTAL_INCREASING`).

Accumulator rules (ported from the PanaSMART power-log design):

- Poll the hourly buckets for yesterday + today at a slow rate (5 minutes).
- Track, per bucket (`usedDate`), the Wh already applied to the meter.
- On every poll add only the positive difference between the bucket value
  reported by LG and the value already applied. Never subtract.
- On the very first poll (no restored state) the existing buckets are used as
  baseline and NOT added, because they may contain energy used before the
  meter was created.
- The applied map is exposed so the integration can persist/restore it across
  restarts; without it, a restart re-baselines and the current bucket's
  energy used during the downtime is lost (never double counted).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
from typing import TYPE_CHECKING, Any

from .core_exceptions import APIError, InvalidRequestError, NotConnectedError
from .device_info import DeviceType, PlatformType

if TYPE_CHECKING:
    from .core_async import ClientAsync
    from .device_info import DeviceInfo

_LOGGER = logging.getLogger(__name__)

# ThinQ v2 service path used by the energy-history endpoint, per device type.
# `laundry` is confirmed for washer / dryer (ollo69/ha-smartthinq-sensors#814).
# Other values are candidates tried in order until one answers; the first
# working path is cached for the device.
ENERGY_SERVICE_PATHS: dict[DeviceType, list[str]] = {
    DeviceType.WASHER: ["laundry"],
    DeviceType.DRYER: ["laundry"],
    DeviceType.TOWER_WASHER: ["laundry"],
    DeviceType.TOWER_DRYER: ["laundry"],
    DeviceType.TOWER_WASHERDRYER: ["laundry"],
    DeviceType.DEHUMIDIFIER: ["aircon", "airpurifier", "dehumidifier"],
}

# Extra query parameters required by some services.
ENERGY_EXTRA_PARAMS: dict[str, dict[str, str]] = {
    "laundry": {"washerType": "M", "twinYn": "N"},
}

# ThinQ1 devices of the air family (AC, dehumidifier: model JSON 401) use
# POST {thinq1Uri}/aircon/inquiryPowerData with
#   {"deviceId": id, "period": "Hour_YYYY.MM.DDT000000Z/YYYY.MM.DDT000000Z"}
# and answer with powerData "HH_MM_Wh/HH_MM_Wh/..." (ioBroker.lg-thinq).
ENERGY_V1_DEVICE_TYPES = [DeviceType.AC, DeviceType.DEHUMIDIFIER]
ENERGY_V1_PATH = "aircon/inquiryPowerData"
ENERGY_V1_KEY_DATA = "powerData"
ENERGY_V1_KEY_RESULT = "returnCd"
ENERGY_V1_EMPTY_RESULT = "0010"
_V1_DATE_FMT = "%Y.%m.%d"

ENERGY_POLL_INTERVAL = 300  # seconds
ENERGY_HISTORY_DAYS = 2  # yesterday + today
ENERGY_MAX_PROBE_FAILURES = 6  # ~30 minutes at 5-minute polls
ENERGY_KEY_ITEMS = "item"
ENERGY_KEY_DATE = "usedDate"
ENERGY_KEY_VALUE = "periodicEnergyData"
_DATE_FMT = "%Y-%m-%d"


def energy_meter_supported(device_info: DeviceInfo) -> bool:
    """
    Return True if an energy meter can be created for the device.

    The energy-history service is a ThinQ2 API, but it is probed for ThinQ1
    devices too: the account gateway always exposes a thinq2Uri and the app
    shows energy monitoring for some ThinQ1 devices. The meter disables
    itself if no service path answers.
    """
    if device_info.platform_type == PlatformType.THINQ1:
        return device_info.type in ENERGY_V1_DEVICE_TYPES
    if device_info.platform_type == PlatformType.UNKNOWN:
        return False
    return device_info.type in ENERGY_SERVICE_PATHS


def parse_energy_history(result: Any) -> dict[str, float] | None:
    """
    Parse an energy-history result into {usedDate: Wh}.

    Return None if the result is not in the expected shape.
    """
    if not isinstance(result, dict):
        return None
    items = result.get(ENERGY_KEY_ITEMS)
    if items is None:
        return None
    if not isinstance(items, list):
        return None
    buckets: dict[str, float] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        used_date = item.get(ENERGY_KEY_DATE)
        value = item.get(ENERGY_KEY_VALUE)
        if not used_date or value is None:
            continue
        try:
            buckets[str(used_date)] = float(value)
        except (TypeError, ValueError):
            _LOGGER.debug("Invalid energy value %s for %s", value, used_date)
            continue
    return buckets


def parse_energy_history_v1(result: Any, day: date) -> dict[str, float] | None:
    """
    Parse a ThinQ1 inquiryPowerData result for `day` into {usedDate: Wh}.

    `powerData` is "HH_MM_Wh/HH_MM_Wh/...". Return an empty dict when the
    server reports no data (returnCd 0010), None if the result is invalid.
    """
    if not isinstance(result, dict):
        return None
    code = result.get(ENERGY_V1_KEY_RESULT)
    if code == ENERGY_V1_EMPTY_RESULT:
        return {}
    if code not in (None, "0000"):
        return None
    data = result.get(ENERGY_V1_KEY_DATA)
    if data is None:
        return {}
    if not isinstance(data, str):
        return None
    prefix = day.strftime(_DATE_FMT)
    buckets: dict[str, float] = {}
    for entry in data.split("/"):
        parts = entry.strip().split("_")
        if len(parts) < 3:
            continue
        try:
            hour = int(parts[0])
            minute = int(parts[1])
            value = float(parts[2])
        except ValueError:
            _LOGGER.debug("Invalid v1 energy entry %s", entry)
            continue
        if hour == 24:
            continue
        buckets[f"{prefix} {hour:02d}:{minute:02d}:00"] = value
    return buckets


class EnergyMeter:
    """Monotonic energy meter fed by the ThinQ energy-history service."""

    def __init__(self, client: ClientAsync, device_info: DeviceInfo) -> None:
        """Initialize the energy meter."""
        self._client = client
        self._device_info = device_info
        self._device_id = device_info.device_id
        self._is_v1 = device_info.platform_type == PlatformType.THINQ1
        if self._is_v1:
            self._paths = (
                [ENERGY_V1_PATH]
                if device_info.type in ENERGY_V1_DEVICE_TYPES
                else []
            )
        else:
            self._paths = list(ENERGY_SERVICE_PATHS.get(device_info.type, []))
        self._service_path: str | None = None
        self._supported = bool(self._paths)

        self._energy_wh: float = 0.0
        self._applied: dict[str, float] = {}
        self._baselined = False  # applied map is trustworthy
        self._has_value = False  # meter has a value (restored or polled)
        self._restored = False
        self._last_poll: datetime | None = None
        self._last_success: datetime | None = None
        self._last_delta_wh: float = 0.0
        self._last_error: str | None = None
        self._raw: dict[str, float] = {}
        self._probe_failures = 0
        self._error_logged = False

    # ---- state exposed to the integration ----

    @property
    def supported(self) -> bool:
        """Return True if the energy meter is (still) considered supported."""
        return self._supported

    @property
    def service_path(self) -> str | None:
        """Return the service path that answered, if any."""
        return self._service_path

    @property
    def energy_kwh(self) -> float | None:
        """Return the meter value in kWh (None before first successful poll)."""
        if not self._has_value:
            return None
        return round(self._energy_wh / 1000.0, 3)

    @property
    def last_delta_wh(self) -> float:
        """Return the Wh added by the last poll."""
        return self._last_delta_wh

    @property
    def last_update(self) -> datetime | None:
        """Return the time of the last successful poll."""
        return self._last_success

    @property
    def last_error(self) -> str | None:
        """Return the last error message."""
        return self._last_error

    @property
    def buckets(self) -> dict[str, float]:
        """Return the last raw buckets {usedDate: Wh}."""
        return dict(self._raw)

    @property
    def applied(self) -> dict[str, float]:
        """Return the Wh already applied per bucket (for persistence)."""
        return dict(self._applied)

    def today_wh(self, today: date | None = None) -> float:
        """Return the sum of today's buckets as reported by LG."""
        day = (today or datetime.now().date()).strftime(_DATE_FMT)
        return sum(v for k, v in self._raw.items() if k.startswith(day))

    def restore(
        self,
        energy_kwh: float | None,
        applied: dict[str, Any] | None = None,
        service_path: str | None = None,
    ) -> None:
        """
        Restore the meter from persisted state.

        Can be called before or after the first poll (Home Assistant restores
        entity state after the coordinator's first refresh). Without a valid
        `applied` map the meter value is restored but the buckets fetched so
        far are used as baseline, so a restart can never double count.
        """
        if self._restored:
            return
        if energy_kwh is None:
            return
        try:
            energy_wh = float(energy_kwh) * 1000.0
        except (TypeError, ValueError):
            return
        self._restored = True
        self._energy_wh = energy_wh
        self._has_value = True
        restored: dict[str, float] = {}
        for key, val in (applied or {}).items():
            try:
                restored[str(key)] = float(val)
            except (TypeError, ValueError):
                continue
        if service_path and service_path in self._paths:
            self._service_path = service_path
        if not restored:
            # keep the current baseline (if any poll already happened)
            return
        self._applied = restored
        self._baselined = True
        if self._raw:
            # a poll already happened: account energy used since shutdown
            self.apply_buckets(self._raw, datetime.now())
        _LOGGER.debug(
            "Energy meter %s restored: %.3f kWh, %d buckets",
            self._device_id,
            energy_kwh,
            len(restored),
        )

    # ---- polling ----

    def _should_poll(self, now: datetime) -> bool:
        if self._last_poll is None:
            return True
        return (now - self._last_poll).total_seconds() >= ENERGY_POLL_INTERVAL

    async def _fetch_v1(self, start: date, end: date) -> dict[str, float] | None:
        """Fetch hourly buckets for each day in [start, end] from the V1 API."""
        buckets: dict[str, float] = {}
        day = start
        while day <= end:
            period = (
                f"Hour_{day.strftime(_V1_DATE_FMT)}T000000Z/"
                f"{(day + timedelta(days=1)).strftime(_V1_DATE_FMT)}T000000Z"
            )
            try:
                result = await self._client.session.post(
                    ENERGY_V1_PATH,
                    {"deviceId": self._device_id, "period": period},
                )
            except APIError as exc:
                if getattr(exc, "code", None) == ENERGY_V1_EMPTY_RESULT:
                    # no data for this day
                    result = {ENERGY_V1_KEY_RESULT: ENERGY_V1_EMPTY_RESULT}
                else:
                    _LOGGER.debug(
                        "Energy history V1 for %s (%s) failed: %s",
                        self._device_id,
                        period,
                        exc,
                    )
                    self._last_error = str(exc)
                    return None
            parsed = parse_energy_history_v1(result, day)
            if parsed is None:
                _LOGGER.debug(
                    "Energy history V1 for %s unexpected result: %s",
                    self._device_id,
                    result,
                )
                self._last_error = "no valid energy history result"
                return None
            buckets.update(parsed)
            day += timedelta(days=1)
        if self._service_path != ENERGY_V1_PATH:
            _LOGGER.info(
                "Energy history for %s (%s) available via ThinQ1 '%s'",
                self._device_id,
                self._device_info.type.name,
                ENERGY_V1_PATH,
            )
            self._service_path = ENERGY_V1_PATH
        return buckets

    async def _fetch(self, start: str, end: str) -> dict[str, float] | None:
        """Fetch buckets trying the candidate service paths."""
        paths = [self._service_path] if self._service_path else self._paths
        last_exc: Exception | None = None
        for path in paths:
            try:
                result = await self._client.session.get_energy_history(
                    self._device_id,
                    path,
                    period="hour",
                    start_date=start,
                    end_date=end,
                    extra_params=ENERGY_EXTRA_PARAMS.get(path),
                )
            except (NotConnectedError, InvalidRequestError, APIError) as exc:
                _LOGGER.debug(
                    "Energy history for %s via '%s' failed: %s",
                    self._device_id,
                    path,
                    exc,
                )
                last_exc = exc
                continue
            buckets = parse_energy_history(result)
            if buckets is None:
                _LOGGER.debug(
                    "Energy history for %s via '%s' unexpected result: %s",
                    self._device_id,
                    path,
                    result,
                )
                continue
            if self._service_path != path:
                _LOGGER.info(
                    "Energy history for %s (%s) available via service '%s'",
                    self._device_id,
                    self._device_info.type.name,
                    path,
                )
                self._service_path = path
            return buckets
        if last_exc is not None:
            self._last_error = str(last_exc)
        else:
            self._last_error = "no valid energy history result"
        return None

    def apply_buckets(self, buckets: dict[str, float], now: datetime) -> float:
        """
        Apply new bucket values to the meter and return the Wh added.

        Pure function of the meter state, kept separate from I/O for testing.
        """
        self._raw = dict(buckets)
        if not self._baselined:
            # baseline: energy already used before the meter existed
            self._applied = dict(buckets)
            self._baselined = True
            self._has_value = True
            self._last_delta_wh = 0.0
            return 0.0

        added = 0.0
        for key, value in buckets.items():
            prev = self._applied.get(key, 0.0)
            delta = value - prev
            if delta > 0:
                added += delta
                self._applied[key] = value
            # never subtract: keep the higher applied value

        # prune applied entries outside the fetched window
        cutoff = (now.date() - timedelta(days=ENERGY_HISTORY_DAYS)).strftime(
            _DATE_FMT
        )
        self._applied = {k: v for k, v in self._applied.items() if k[:10] > cutoff}

        self._energy_wh += added
        self._last_delta_wh = added
        return added

    async def async_update(self, force: bool = False) -> bool:
        """Poll the energy history if due. Return True if the meter changed."""
        if not self._supported:
            return False
        now = datetime.now(timezone.utc)
        if not force and not self._should_poll(now):
            return False
        self._last_poll = now

        local_now = datetime.now()
        start_day = local_now.date() - timedelta(days=ENERGY_HISTORY_DAYS - 1)
        end_day = local_now.date()
        if self._is_v1:
            buckets = await self._fetch_v1(start_day, end_day)
        else:
            buckets = await self._fetch(
                start_day.strftime(_DATE_FMT), end_day.strftime(_DATE_FMT)
            )
        if buckets is None:
            if not self._error_logged:
                self._error_logged = True
                _LOGGER.info(
                    "Energy history for %s (%s) not available yet: %s",
                    self._device_id,
                    self._device_info.type.name,
                    self._last_error,
                )
            if self._service_path is None and self._last_success is None:
                # no candidate path ever answered
                self._probe_failures += 1
                if self._probe_failures >= ENERGY_MAX_PROBE_FAILURES:
                    _LOGGER.info(
                        "Energy history not available for %s (%s) after %d attempts,"
                        " disabling (last error: %s)",
                        self._device_id,
                        self._device_info.type.name,
                        self._probe_failures,
                        self._last_error,
                    )
                    self._supported = False
            return False

        added = self.apply_buckets(buckets, local_now)
        self._last_success = now
        self._last_error = None
        if added:
            _LOGGER.debug(
                "Energy meter %s: +%.0f Wh -> %.3f kWh",
                self._device_id,
                added,
                self._energy_wh / 1000.0,
            )
        return True
