# Changelog

## 1.3.0

Requires Home Assistant 2026.5 or newer. No reconfiguration is needed. On the first refresh after updating, each energy exchange backfills its monthly history (walking back up to five calendar years); afterwards, refreshes only fetch current data. Historical monthly series appear as new **(shared energy)** sources for the Energy dashboard; consumption and injection statistics are never touched.

- Backfill monthly energiedelen (energy sharing) history per energy exchange, including previous calendar years, and import it as cumulative monthly external statistics that follow Fluvius corrections ([#28](https://github.com/sander110419/Fluvius-home-assistant/issues/28)).
- Disambiguate sensors when an account has multiple energy exchanges: the exchange name and ID are appended to the sensor name (e.g. `(Vriendengroep #62338)`).
- Expose the full monthly history as a `monthly_history_kwh` attribute on the "this year" sharing sensors.

Validated with 67 tests on Home Assistant 2026.5 and a running Home Assistant instance, verifying that existing statistic IDs and rows are never overwritten for accounts without energy sharing.

## 1.2.0

Requires Home Assistant 2026.5 or newer. No reconfiguration is needed: token storage is added automatically on the first refresh after updating. Accounts participating in energiedelen (energy sharing) get new sensors after the restart; all other accounts see no new entities.

- Renew access tokens via the Azure B2C refresh token grant instead of a full credential login on every hourly poll, and persist tokens across restarts.
- Only trigger reauthentication when Fluvius explicitly rejects the credentials. Transient portal or login failures now retry silently on the next refresh, ending the recurring "credentials expired" notifications reported in [sander110419/Fluvius_API#5](https://github.com/sander110419/Fluvius_API/issues/5).
- Add energiedelen (energy sharing) support: for each energy exchange, six sensors report shared energy received/injected today, this month, and this year in kWh ([#28](https://github.com/sander110419/Fluvius-home-assistant/issues/28)).
- Align gas interval requests on the 06:00-06:00 gas day, so detailed gas readings spread over their real hours instead of collapsing into a single 06:00 spike. Thanks [@cdamman](https://github.com/cdamman) ([#25](https://github.com/sander110419/Fluvius-home-assistant/pull/25)).

Validated with 58 tests on Home Assistant 2026.5, live full-login and refresh-grant authentication against the production portal, and a running Home Assistant instance. The energiedelen endpoints follow the Mijn Fluvius frontend contract and were verified for accounts without energy sharing; feedback from energiedelen participants is welcome.

## 1.1.0

Requires Home Assistant 2026.5 or newer. After updating, restart Home Assistant and select the new **(historical)** sources in the Energy dashboard. Old sensor history is not automatically rewritten.

- Import delayed consumption/injection at its actual measurement time, using hourly external statistics.
- Correct the electricity quarter-hour API code and fetch every day in the configured lookback window.
- Add gas hourly requests and options, gas-unit selection during setup, and valid gas sensor classes.
- Deduplicate imports across restarts and entry recreation; apply both upward and downward corrections.
- Preserve tariff breakdowns, handle daylight-saving changes, and replace daily allocations with complete interval data.
- Add an exclusive history cutoff for combining a separate Fluvius history source with a P1 meter.
- Preserve available display data on empty refreshes, isolate peak endpoint failures, cache access tokens, and trigger reauthentication when credentials fail.
- Fix option labels and verbose-logging propagation; add automated regression checks.

Validated with 41 tests on Home Assistant 2026.5, actual SQLite Recorder imports, seven days of live electricity readings, and a running Home Assistant instance. Gas daily readings were verified live; hourly gas was verified with fixtures because the test account returned no hourly data.

Professional eID/itsme login and exact 15-minute Recorder/dynamic-price accounting remain unsupported. See the README for details and upgrade instructions.
