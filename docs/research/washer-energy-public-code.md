# Washer energy: public code and API research

Investigated 2026-10-24 (Asia/Taipei). Target: Taiwan ThinQ2 top-loader `Ht16_F`, model type `TL`. This note distinguishes observations from this device, primary public source findings, and unresolved questions. It does not establish a physical-energy conversion for this model.

## Conclusions

1. Hourly laundry history must be fetched one day per request. This is corroborated by another implementation, its docs, an app HTTP capture, and our live device responses.
2. `power` and `courseSpendPower` cannot safely replace `periodicEnergyData` as Wh for this model: its legacy LG app module explicitly maps cycle consumption into six levels. A modern physical-energy app view can coexist with that legacy module.
3. The official ThinQ Connect API is a separate, capability-aware energy route worth comparing. Its profile can identify supported energy properties; its response has a different schema and authentication from the private app API.
4. Neither public code reviewed nor our live results explains why some completed cycles have zero `periodicEnergyData`. A zero does not establish that the washer lacks energy support.

## Primary public sources

### Private laundry API: captured app request

An owner of a WM6700HBA captured `service/laundry/{id}/energy-history` with `type=period`, `period=hour`, identical start/end dates, `washerType=M`, and `twinYn=N`. The capture reports hourly `periodicEnergyData` matching app energy while `power` differs; e.g. one bucket has 383 versus 232. Thus field substitution is not generally valid. The captured host is the account's US `aic-service` host, not proof that the Taiwan device should use it. [Original capture, issue #814](https://github.com/ollo69/ha-smartthinq-sensors/issues/814).

### Independent implementation: ioBroker

`sendStaticRequest` rejects hourly queries when start/end differ. It sends `hour`, `day`, `month`, or `year`; a nearby comment says year is unsupported and should be changed to month, but the reviewed code still sends `year`. Our own `week` and `year` requests failed, while `day` and `month` succeeded. Laundry requests use the same `service/laundry/{id}/energy-history` route. This independently supports the date-range defect observed locally. [ioBroker helper.js, around lines 1042–1087](https://github.com/TA2k/ioBroker.lg-thinq/blob/master/lib/helper.js#L1042).

The washer docs likewise require equal hourly dates. Its washer example has small daily values: two cycles with `power=2`, `periodicEnergyData=2`, and `powerWh=0`; another day has all three count/power/periodic values equal to 5. These examples demonstrate the schema is used for low-valued legacy data, but do not define its units. Do not extrapolate a conversion from a different appliance example. [ioBroker washer statistics documentation](https://github.com/TA2k/ioBroker.lg-thinq/blob/master/docs/en/README.md#201-remote-statistic-thinq2).

### Modern and legacy app views coexist

A GC3V708S2 owner reported in November 2024 that a newer app added hourly/daily/weekly/monthly physical energy views in Wh/kWh, replacing the older bar-only presentation. This is a firsthand report, not an API specification, and concerns a different model. It supports the user's correction that inspecting only an old model module cannot characterize every current app view. [Original report, issue #827](https://github.com/ollo69/ha-smartthinq-sensors/issues/827).

### Official LG ThinQ Connect API

LG's own `pythinqconnect` implements a distinct host `https://api-{region}.lgthinq.com/`, Bearer token authentication, and `x-country`, `x-client-id`, `x-api-key`, `x-message-id`, and service-phase headers. Energy routes are:

- `GET devices/energy/{deviceId}/profile`
- `GET devices/energy/{deviceId}/usage?property={property}&period={period}&startDate={start}&endDate={end}`

This differs from private `service/laundry/.../energy-history`; the app API's refresh token should not be assumed interchangeable with a Connect PAT. [LG-authored API client, lines 133–195](https://github.com/thinq-connect/pythinqconnect/blob/main/thinqconnect/thinq_api.py#L133).

Taiwan maps to KIC, so its Connect host is `api-kic.lgthinq.com`. [LG country mapping](https://github.com/thinq-connect/pythinqconnect/blob/main/thinqconnect/country.py#L185).

The official device base class reads supported energy property names from `energy_profile.result.property`. Its periods are `DAILY` and `MONTHLY`, with `YYYYMMDD` and `YYYYMM` dates respectively. It validates dates and property support rather than assuming every washer has energy. [LG device implementation, lines 321–331 and 446–497](https://github.com/thinq-connect/pythinqconnect/blob/main/thinqconnect/devices/connect_device.py#L321).

The Home Assistant bridge fetches an optional energy profile at device setup. Its usage method reads `result.dataList` and sums the requested property, with no legacy `power` fallback. [LG Home Assistant bridge, lines 848–885 and 929–943](https://github.com/thinq-connect/pythinqconnect/blob/main/thinqconnect/integration/homeassistant/api.py#L848).

Current Home Assistant core describes energy sensors in Wh and creates them from supported energy property IDs. The reviewed development source has today/yesterday/month sensors; released documentation lists yesterday/current month/last month and says aggregation through yesterday updates each morning. These are different source versions, so do not assume a released installation has the development behavior. [HA sensor implementation](https://github.com/home-assistant/core/blob/dev/homeassistant/components/lg_thinq/sensor.py#L596), [HA integration documentation](https://www.home-assistant.io/integrations/lg_thinq/#energy-consumption).

## Exact-device evidence (local first-party artifacts)

The user-supplied diagnostics identify `Ht16_F`, `modelType=TL`, and `EnergyMonitoring.powertable` mapping 1 through 6 to themselves. The app module linked by that device's `appModuleUri` was downloaded into `/private/tmp/washer-app-module.bin` for inspection. In its `wpm/GWM/merge.js`, `GWM_ENM01_Main/201` maps `courseSpendPower` through `getUsageLevel(powertable, value)` and renders six level icons. Its product language pack (`/private/tmp/langPackProductTypeUri.json`, inspected by the primary agent) supplies accessibility labels describing consumption as a value out of a number of levels, and a disclaimer describing relative indicators under comparable laundry/default conditions.

These artifacts support treating legacy cycle `4` as a level in that UI. They do not prove the current app's separate physical energy view is unsupported, nor determine the unit of every `periodicEnergyData` response.

Live queries reported in this session:

| Query | count | power | periodicEnergyData | powerWh |
| --- | ---: | ---: | ---: | ---: |
| Oct 19, hourly | 1 | 4 | 4 | 0 |
| Oct 22, hourly | 1 | 4 | 0 | 0 |
| Oct 24, hourly/day | 1 | 4 | 0 | 0 |
| October daily sum/month | 19 | 63 | 46 | 0 |

The combined hourly query Oct 23–24 returned 24 buckets for Oct 23 only. Single-day Oct 24 returned the completed cycle at 18:51:57 local time in the 18:00 bucket. Removing optional washer parameters did not change today's result. October daily data also contains older missing/partial values, including Oct 8 (0 versus 4) and Oct 10 (1 versus 5); this rules out explaining the entire discrepancy by the current cycle still running.

### Official Connect API comparison attempted

The mounted Home Assistant configuration also has an existing official `lg_thinq` entry. Its saved credentials were used only for read-only requests; no token values or identifiers are included here, and configuration was not changed.

- `GET /devices` on `api-kic.lgthinq.com` succeeded (HTTP 200), listing a washer, dryer, and dehumidifier.
- Connect's device identifier differs from the private API UUID. The washer was selected from the official response before the energy-profile check.
- `GET /devices/energy/{officialWasherId}/profile` returned HTTP 401, LG code `1309`. The SDK names this error `NOT_ALLOWED_API_AGAIN`; the result indicates denial for this call, not proof that the washer lacks energy support. [LG error definitions](https://github.com/thinq-connect/pythinqconnect/blob/main/thinqconnect/thinq_api.py#L55).

The normal listing succeeding separates this from a completely invalid token. Missing energy scope is a plausible explanation, but service/account policy has not been excluded. Home Assistant's official setup instructions explicitly require the energy-consumption inquiry permission. Verify that permission before concluding the official energy route is unavailable. No token replacement or permission changes were attempted. [Required PAT scopes](https://www.home-assistant.io/integrations/lg_thinq/#personal-access-token-pat).

## What remains unresolved

- Exact request/response used by the current physical energy app view for this account/model.
- Physical units, if any, of this model's tiny legacy fields and whether `periodicEnergyData` is reliably measured energy here.
- Whether Connect exposes energy for this exact washer and whether its historical usage agrees with the phone app. The existing token's energy-profile call was denied; a successful authorized profile/usage comparison is still needed.
- Why historical per-cycle and periodic values differ. No confirmed public fix for this exact pattern was found in the reviewed sources.

## Same-account device comparison, 20:51 Taipei time

A read-only Python probe compared the three registered appliances on the same dates. Sanitized output is saved in [energy-device-comparison-2026-10-24.json](energy-device-comparison-2026-10-24.json). Values below are raw sums, not independently calibrated measurements.

| Device | Platform | Date | periodicEnergyData | power | Cycles |
| --- | --- | --- | ---: | ---: | ---: |
| Washer | ThinQ2 | Oct 24 | 0 | 4 | 1 |
| Dryer | ThinQ2 | Oct 24 | 906 | 906 | 1 |
| Washer | ThinQ2 | Oct 22 | 0 | 4 | 1 |
| Dryer | ThinQ2 | Oct 22 | 1086 | 1086 | 1 |
| Washer | ThinQ2 | Oct 19 | 4 | 4 | 1 |
| Dryer | ThinQ2 | Oct 19 | 902 | 902 | 1 |

Both ThinQ2 devices return only Oct 23 when requested for hourly Oct 23–24; each returns today's records when queried for Oct 24 alone. Thus the dryer also has the date-range problem. It supplies meaningful-looking nonzero hourly energy, unlike the washer's missing/tiny values. The comparison establishes a model-specific data difference on the same API; it does not establish the washer's physical units.

The dehumidifier is confirmed ThinQ1 and uses a different endpoint. A follow-up raw request returned:

```text
Oct 24: 18_0_1/19_0_6/20_96_3
Oct 22 final entry: 23_555_2
Oct 19 final entry: 20_360_2
```

The current parser treats the second component as minutes and the third as Wh. This produces impossible bucket keys (`20:96`, `23:555`, `20:360`), so describing the dehumidifier meter as verified correct was premature. Its computed totals in the JSON are current-parser outputs, not verified Wh. The exact meaning of the second and third components needs first-party verification; do not simply swap them based on these examples. ioBroker uses the same second-as-minutes assumption, which is insufficient validation for these actual responses. [ioBroker V1 parser](https://github.com/TA2k/ioBroker.lg-thinq/blob/master/lib/helper.js#L956).

## Next implementation boundaries

Fetch hourly data as separate days; preserve partial successes and monotonic restoration behavior. Do not add a `power` fallback or rescale legacy levels without matching physical units to the current app or official energy profile. Treat the date correction and unresolved energy semantics as separate findings.

## Dehumidifier format resolved from its own LG app module

Subsequent inspection of model `DHUM_056905_WW`'s downloaded app module (`/private/tmp/dehum-appModuleUri.bin`, ZIP member `wrm/GJM/GJM.bundle.js`) resolves the earlier uncertainty. Its `getENMProcessingData` reads the first component as the hourly index, the second as `operationTime`, and the third as energy `value`. Its display sums those values as Wh and divides by 1000 for kWh. Thus `20_96_3` means hour 20, operation time 96, energy 3 Wh. The operation-time unit was not established and is not needed by the meter.

The parser's energy extraction was correct, but its key incorrectly included operation time as minutes. Changing operation time could create a new bucket and count an hour's energy again. The local parser now uses stable `HH:00:00` keys, and restored ThinQ1 applied maps normalize old keys to the hour, retaining the maximum applied value when old keys collide. Existing accumulated totals are preserved; past overcount cannot be reliably reconstructed from the retained window.

Two regression tests failed before the correction and pass afterward: stable-hour delta accounting using the captured `20_96_3` response, and migration of restored operation-time keys. All 15 standalone energy test functions pass. Replaying the captured responses yields 10 Wh (Oct 24), 55 Wh (Oct 22), and 38 Wh (Oct 19), now with valid hourly keys. These totals follow LG's app interpretation; independent physical calibration was not performed. Full pytest could not run in the temporary environment because pytest is not installed. Changes are local to the repository and have not been deployed to Home Assistant.

## Confirmed corrections completed locally

Laundry hourly polling now fetches each day separately and merges successful responses. Tests cover both washer and dryer, positive deltas without recounting, a failure on either day, and failure on every day. The temporary raw-response logging added during investigation was removed. All 18 standalone energy tests pass; full pytest remains unavailable in the temporary environment.

A live check through the corrected code returned 48 buckets covering Oct 23 and 24 for each laundry device. The dryer sum was 906 Wh; the washer's parsed sum remained zero. The corrected dehumidifier code returned three valid hourly keys totaling 11 Wh at the later check (up from 10 Wh earlier). No Home Assistant configuration or installed integration files were changed.

Remaining evidence required:

1. A screenshot of the current app's washer energy view for Oct 22 or Oct 24, including the number, unit, and selected date. This anchors the expected physical value; a nonzero bar alone does not establish Wh.
2. An authorized official Connect energy-profile response and, if supported, daily usage for the same dates. The configured token can list devices but is denied energy-profile access with code 1309. Verify the energy-inquiry scope and retry; do not assume a different error cause is ruled out.
3. If official usage is unavailable or differs from the app, the app's energy-view request/response with credentials redacted. Required fields: host/path, date/period parameters, relevant non-secret headers, and energy values/units. This establishes which endpoint/field the modern view actually uses.
4. If historical dehumidifier totals must be repaired, pre-fix Home Assistant sensor history or backups plus adequate raw LG energy history to reconstruct the overcount. The retained applied map alone cannot recover earlier inflation, so the fix preserves the accumulated total.

## User-directed provisional washer implementation

The user explicitly requested retaining the washer energy meter, using `power` on a best-effort basis, and validating over subsequent days. This supersedes the earlier recommendation to wait for physical-unit verification before implementing a fallback; it does not resolve the unit uncertainty.

For ThinQ2 `DeviceType.WASHER`, a positive finite `periodicEnergyData` is preferred. When it is zero, missing or invalid, a positive finite `power` is used, provisionally at one raw unit per Wh. Dryer and dehumidifier do not use this fallback. The same hourly keys and applied-value map prevent summing both sources or counting repeated polls/restarts twice. If LG later reports a larger periodic value, only the positive difference is added; a smaller value cannot reduce the total.

The original entity, accumulated total, and restore behavior remain. Washer attributes `energy_estimated=true`, `power_fallback_assumed_unit=Wh`, and `power_fallback_buckets` make the policy and actual fallback buckets inspectable. `energy_estimated` remains true for a washer using this policy even when the current window contains no fallback, because its cumulative total can contain earlier estimates. This flag denotes the unverified policy, not a claim that every periodic value is estimated.

The live probe through this implementation returned a provisional 4 Wh for the washer's current window, 906 Wh for the dryer, and 13 Wh for the dehumidifier at the later query. All 22 standalone energy test functions pass, including reported-energy preference, fallback provenance, later backfill, restart deduplication, appliance isolation, and invalid fallback values. Full Home Assistant tests remain unavailable in the temporary environment. These changes are local, not deployed.

For validation after installation, record the washer meter, `today_wh`, `power_fallback_buckets`, and completed cycles for several days. With the current 1:1 assumption a cycle reporting power 4 contributes 0.004 kWh; a larger calibration factor has not been invented. Matching the app's unitless bars validates activity/relative values only. Physical accuracy still needs a known-unit measurement, such as independent plug energy over the same cycles or authorized official energy usage. No scheduled task was created.

## Deployment

The tested `wideq/energy.py` and `sensor.py` were deployed to the mounted Home Assistant configuration on 2026-10-24 at 21:40 Taipei time, with the previous files backed up outside the integration directory. File contents and Python syntax were verified after copying. The user subsequently confirmed completing the requested restart. Runtime behavior following that restart and multi-day washer calibration have not yet been verified; earlier statements that changes were local describe the investigation before this deployment.
