import asyncio, sys, types
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

def test_disable_after_failures():
    sess = FakeSession("nope", [SAMPLE])
    m = EnergyMeter(FakeClient(sess), DI())
    for _ in range(ENERGY_MAX_PROBE_FAILURES):
        assert m.supported
        asyncio.run(m.async_update(force=True))
    assert not m.supported
    assert m.last_error
