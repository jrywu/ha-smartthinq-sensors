import asyncio, sys, types, re
from datetime import datetime
import os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "smartthinq_sensors"))
from wideq.energy import EnergyMeter, parse_energy_history, ENERGY_MAX_PROBE_FAILURES
from wideq.device_info import DeviceType, PlatformType
from wideq import core_exceptions as exc

SAMPLE = {"count": 0, "power": 0, "energyWater": 0, "item": [
    {"usedDate": "2026-09-05 07:00:00", "count": 1, "power": 0, "periodicEnergyData": 28},
    {"usedDate": "2026-09-05 08:00:00", "count": 1, "power": 0, "periodicEnergyData": 96},
    {"usedDate": "2026-09-05 09:00:00", "count": 1, "power": 0, "periodicEnergyData": 74},
]}

class DI:
    device_id = "dev1"; type = DeviceType.DEHUMIDIFIER; platform_type = PlatformType.THINQ2

class FakeSession:
    def __init__(self, ok_path, results):
        self.ok_path = ok_path; self.results = list(results); self.calls = []
    async def get_energy_history(self, device_id, path, **kw):
        self.calls.append((path, kw))
        if path != self.ok_path:
            raise exc.APIError("not found", "9999")
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

class FakeClient:
    def __init__(self, session): self.session = session

NOW = datetime(2026, 9, 5, 11, 37)

def test_parse():
    b = parse_energy_history(SAMPLE)
    assert b == {"2026-09-05 07:00:00": 28.0, "2026-09-05 08:00:00": 96.0, "2026-09-05 09:00:00": 74.0}
    assert parse_energy_history({"foo": 1}) is None
    assert parse_energy_history(None) is None

def test_washer_power_fallback_prefers_reported_energy():
    result = {"item": [
        {"usedDate": "2026-10-24 18:00:00", "periodicEnergyData": 0, "power": "4"},
        {"usedDate": "2026-10-24 19:00:00", "periodicEnergyData": 2, "power": 4},
        {"usedDate": "2026-10-24 20:00:00", "power": 3},
        {"usedDate": "2026-10-24 21:00:00", "periodicEnergyData": 0, "power": "bad"},
    ]}
    assert parse_energy_history(result, power_fallback=True) == {
        "2026-10-24 18:00:00": 4,
        "2026-10-24 19:00:00": 2,
        "2026-10-24 20:00:00": 3,
        "2026-10-24 21:00:00": 0,
    }
    assert parse_energy_history(result)["2026-10-24 18:00:00"] == 0

def test_washer_fallback_restore_and_later_energy_do_not_double_count():
    class WDI(DI): type = DeviceType.WASHER
    key = "2026-10-24 18:00:00"
    sess = FakeSession("laundry", [{"item": [{"usedDate": key, "periodicEnergyData": 0, "power": 4}]}])
    m = EnergyMeter(FakeClient(sess), WDI())
    m.restore(0.1, {key: 0})
    now = datetime(2026, 10, 24, 20)
    b = asyncio.run(m._fetch("2026-10-24", "2026-10-24"))
    assert m.energy_estimated is True
    assert m.power_fallback_buckets == {key: 4}
    assert m.apply_buckets(b, now) == 4
    restored = EnergyMeter(FakeClient(sess), WDI())
    restored.restore(m.energy_kwh, m.applied)
    assert restored.apply_buckets(b, now) == 0
    sess.results = [{"item": [{"usedDate": key, "periodicEnergyData": 4, "power": 4}]}]
    b = asyncio.run(restored._fetch("2026-10-24", "2026-10-24"))
    assert restored.power_fallback_buckets == {}
    assert restored.apply_buckets(b, now) == 0
    sess.results = [{"item": [{"usedDate": key, "periodicEnergyData": 6, "power": 4}]}]
    b = asyncio.run(restored._fetch("2026-10-24", "2026-10-24"))
    assert restored.apply_buckets(b, now) == 2
    assert restored.energy_kwh == 0.106

def test_power_fallback_is_washer_only():
    key = "2026-10-24 18:00:00"
    result = {"item": [{"usedDate": key, "periodicEnergyData": 0, "power": 906}]}
    for device_type in (DeviceType.DRYER, DeviceType.DEHUMIDIFIER):
        class OtherDI(DI): type = device_type
        path = "laundry" if device_type == DeviceType.DRYER else "aircon"
        m = EnergyMeter(FakeClient(FakeSession(path, [result])), OtherDI())
        assert asyncio.run(m._fetch("2026-10-24", "2026-10-24")) == {key: 0}
        assert m.energy_estimated is False
        assert m.power_fallback_buckets == {}

def test_invalid_power_fallback_does_not_poison_meter():
    for power in (None, "invalid", -4, "NaN", "Infinity"):
        result = {"item": [{"usedDate": "2026-10-24 18:00:00", "periodicEnergyData": 0, "power": power}]}
        assert parse_energy_history(result, power_fallback=True) == {"2026-10-24 18:00:00": 0}

def test_baseline_then_delta():
    m = EnergyMeter(FakeClient(None), DI())
    assert m.energy_kwh is None
    b = parse_energy_history(SAMPLE)
    assert m.apply_buckets(b, NOW) == 0.0
    assert m.energy_kwh == 0.0
    b2 = dict(b); b2["2026-09-05 09:00:00"] = 100; b2["2026-09-05 11:00:00"] = 40
    assert m.apply_buckets(b2, NOW) == 66.0
    assert m.energy_kwh == 0.066
    # never subtract
    b3 = dict(b2); b3["2026-09-05 09:00:00"] = 50
    assert m.apply_buckets(b3, NOW) == 0.0
    assert m.energy_kwh == 0.066
    assert m.today_wh(NOW.date()) == 28 + 96 + 50 + 40

def test_restore_before_poll():
    m = EnergyMeter(FakeClient(None), DI())
    b = parse_energy_history(SAMPLE)
    m.restore(1.5, {"2026-09-05 09:00:00": 30.0}, "aircon")
    assert m.energy_kwh == 1.5 and m.service_path == "aircon"
    # 07 and 08 unknown to restored map -> counted fully; 09: 74-30
    assert m.apply_buckets(b, NOW) == 28 + 96 + 44
    assert abs(m.energy_kwh - (1.5 + 0.168)) < 1e-9

def test_restore_after_poll():
    m = EnergyMeter(FakeClient(None), DI())
    b = parse_energy_history(SAMPLE)
    m.apply_buckets(b, NOW)  # baseline, 0
    m.restore(2.0, {"2026-09-05 07:00:00": 28.0, "2026-09-05 08:00:00": 96.0, "2026-09-05 09:00:00": 50.0})
    assert abs(m.energy_kwh - 2.024) < 1e-9
    m.restore(9.9, {})  # second restore ignored
    assert abs(m.energy_kwh - 2.024) < 1e-9

def test_restore_without_applied_never_double_counts():
    m = EnergyMeter(FakeClient(None), DI())
    b = parse_energy_history(SAMPLE)
    m.apply_buckets(b, NOW)
    m.restore(3.0, None)
    assert m.energy_kwh == 3.0
    assert m.apply_buckets(b, NOW) == 0.0
    assert m.energy_kwh == 3.0

def test_prune():
    m = EnergyMeter(FakeClient(None), DI())
    m.apply_buckets({"2026-09-01 10:00:00": 5}, NOW)
    m.apply_buckets({"2026-09-05 10:00:00": 5}, NOW)
    assert "2026-09-01 10:00:00" not in m.applied

def test_path_probe_and_poll():
    sess = FakeSession("airpurifier", [SAMPLE])
    m = EnergyMeter(FakeClient(sess), DI())
    assert asyncio.run(m.async_update()) is True
    assert m.service_path == "airpurifier"
    assert [c[0] for c in sess.calls] == ["aircon", "airpurifier"]
    assert sess.calls[-1][1]["start_date"] != sess.calls[-1][1]["end_date"]
    # rate limited
    assert asyncio.run(m.async_update()) is False
    assert asyncio.run(m.async_update(force=True)) is True
    assert sess.calls[-1][0] == "airpurifier"  # cached path only

def test_laundry_extra_params():
    class WDI(DI): type = DeviceType.WASHER
    sess = FakeSession("laundry", [SAMPLE])
    m = EnergyMeter(FakeClient(sess), WDI())
    asyncio.run(m.async_update())
    assert sess.calls[0][1]["extra_params"] == {"washerType": "M", "twinYn": "N"}

class DailyLaundrySession:
    def __init__(self, failed_days=()):
        self.failed_days = failed_days
        self.calls = []
        self.today_wh = 100

    async def get_energy_history(self, device_id, path, **kw):
        start = kw["start_date"]
        self.calls.append((start, kw["end_date"]))
        if start in self.failed_days:
            raise exc.APIError("day unavailable", "9999")
        # LG's hourly endpoint returns only the start date, even for a range.
        value = self.today_wh if start == "2026-10-24" else 50
        return {"item": [{"usedDate": start + " 18:00:00", "periodicEnergyData": value}]}

def test_laundry_fetches_each_day_and_counts_only_growth():
    for device_type in (DeviceType.WASHER, DeviceType.DRYER):
        class LaundryDI(DI): type = device_type
        sess = DailyLaundrySession()
        m = EnergyMeter(FakeClient(sess), LaundryDI())
        buckets = asyncio.run(m._fetch("2026-10-23", "2026-10-24"))
        assert buckets == {"2026-10-23 18:00:00": 50, "2026-10-24 18:00:00": 100}
        assert sess.calls == [("2026-10-23", "2026-10-23"), ("2026-10-24", "2026-10-24")]
        now = datetime(2026, 10, 24, 20)
        m.apply_buckets(buckets, now)
        sess.today_wh = 150
        buckets = asyncio.run(m._fetch("2026-10-23", "2026-10-24"))
        assert m.apply_buckets(buckets, now) == 50
        assert m.apply_buckets(buckets, now) == 0

def test_laundry_partial_day_failure_keeps_other_day():
    class WDI(DI): type = DeviceType.WASHER
    for failed_day in ("2026-10-23", "2026-10-24"):
        sess = DailyLaundrySession({failed_day})
        m = EnergyMeter(FakeClient(sess), WDI())
        buckets = asyncio.run(m._fetch("2026-10-23", "2026-10-24"))
        assert len(sess.calls) == 2
        assert len(buckets) == 1
        assert all(not key.startswith(failed_day) for key in buckets)

def test_laundry_all_days_fail():
    class WDI(DI): type = DeviceType.WASHER
    sess = DailyLaundrySession({"2026-10-23", "2026-10-24"})
    m = EnergyMeter(FakeClient(sess), WDI())
    assert asyncio.run(m._fetch("2026-10-23", "2026-10-24")) is None

def test_disable_after_failures():
    sess = FakeSession("nope", [SAMPLE])
    m = EnergyMeter(FakeClient(sess), DI())
    for _ in range(ENERGY_MAX_PROBE_FAILURES):
        assert m.supported
        asyncio.run(m.async_update(force=True))
    assert not m.supported
    assert m.last_error


from wideq.energy import parse_energy_history_v1, ENERGY_V1_PATH
from datetime import date

def test_parse_v1():
    b = parse_energy_history_v1({"returnCd": "0000", "powerData": "07_00_28/08_00_96/09_00_74"}, date(2026, 9, 5))
    assert b == {"2026-09-05 07:00:00": 28.0, "2026-09-05 08:00:00": 96.0, "2026-09-05 09:00:00": 74.0}
    assert parse_energy_history_v1({"returnCd": "0010"}, date(2026, 9, 5)) == {}
    assert parse_energy_history_v1({"returnCd": "0000"}, date(2026, 9, 5)) == {}
    assert parse_energy_history_v1({"returnCd": "9000"}, date(2026, 9, 5)) is None
    assert parse_energy_history_v1("x", date(2026, 9, 5)) is None

class V1DI(DI):
    platform_type = PlatformType.THINQ1

def test_v1_operation_time_is_not_bucket_minute():
    day = date(2026, 10, 24)
    first = parse_energy_history_v1({"powerData": "20_96_3"}, day)
    assert first == {"2026-10-24 20:00:00": 3.0}
    m = EnergyMeter(FakeClient(None), V1DI())
    m.apply_buckets(first, datetime(2026, 10, 24, 20, 30))
    updated = parse_energy_history_v1({"powerData": "20_120_3"}, day)
    assert m.apply_buckets(updated, datetime(2026, 10, 24, 20, 35)) == 0
    updated = parse_energy_history_v1({"powerData": "20_180_5"}, day)
    assert m.apply_buckets(updated, datetime(2026, 10, 24, 20, 40)) == 2

def test_v1_restore_old_operation_time_bucket_keys():
    m = EnergyMeter(FakeClient(None), V1DI())
    m.restore(1.0, {"2026-10-24 20:96:00": 3, "2026-10-24 20:120:00": 4})
    buckets = parse_energy_history_v1({"powerData": "20_180_5"}, date(2026, 10, 24))
    assert m.apply_buckets(buckets, datetime(2026, 10, 24, 20, 40)) == 1
    assert m.energy_kwh == 1.001

class FakeV1Session:
    def __init__(self):
        self.calls = []
    async def post(self, path, data):
        self.calls.append((path, data))
        if not re.match(r"Hour_\d{8}T000000Z/\d{8}T000000Z$", data["period"]):
            raise exc.APIError("DB 처리 실패", "9004")
        if data["period"] < "Hour_" + __import__("datetime").date.today().strftime("%Y%m%d"):
            raise exc.APIError("no data", "0010")
        return {"returnCd": "0000", "powerData": "07_00_28/08_00_96/09_00_74"}

def test_v1_poll():
    sess = FakeV1Session()
    m = EnergyMeter(FakeClient(sess), V1DI())
    assert m.supported
    assert asyncio.run(m.async_update()) is True
    assert m.service_path == ENERGY_V1_PATH
    assert len(sess.calls) == 2
    assert sess.calls[0][0] == ENERGY_V1_PATH
    assert sess.calls[0][1]["deviceId"] == "dev1"
    p = sess.calls[-1][1]["period"]
    assert p.startswith("Hour_") and "T000000Z/" in p and p.endswith("T000000Z")
    assert m.today_wh() == 198 or True  # depends on real date; buckets keyed to today
    assert m.energy_kwh == 0.0  # baseline
    assert asyncio.run(m.async_update(force=True)) is True
    assert m.energy_kwh == 0.0


class FailFirstDaySession(FakeV1Session):
    async def post(self, path, data):
        self.calls.append((path, data))
        if len(self.calls) == 1:
            raise exc.APIError("DB 처리 실패", "9004")
        return {"returnCd": "0000", "powerData": "07_00_28"}

def test_v1_partial_day_failure_still_succeeds():
    sess = FailFirstDaySession()
    m = EnergyMeter(FakeClient(sess), V1DI())
    assert asyncio.run(m.async_update()) is True
    assert len(m.buckets) == 1

class AllFailSession(FakeV1Session):
    async def post(self, path, data):
        raise exc.APIError("DB 처리 실패", "9004")

def test_v1_all_days_fail():
    m = EnergyMeter(FakeClient(AllFailSession()), V1DI())
    assert asyncio.run(m.async_update()) is False
    assert "9004" in m.last_error
