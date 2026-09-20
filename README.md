# Fluvius Energy for Home Assistant

Import electricity and gas consumption from Mijn Fluvius into Home Assistant. Fluvius publishes readings after the energy was used, often one or more days later. This integration polls hourly and imports historical statistics at the original measurement times.

## Requirements and installation

- Home Assistant **2026.5 or newer**, with Recorder enabled.
- A **personal** Mijn Fluvius account that supports email/password login, the meter's EAN and serial number, and access to its consumption history.
- For detailed history, enable electricity quarter-hour or gas hourly readings in Mijn Fluvius. Permission given to an energy supplier alone does not guarantee that those readings are available to your portal account.

Install through HACS, or copy `custom_components/fluvius` to your configuration directory and restart Home Assistant. Add **Fluvius Energy** under **Settings → Devices & services**. Create one entry per EAN. For gas, select **kWh** or **m3** during setup, before the first import.

Professional accounts requiring eID/itsme cannot use the password login in this integration. Fluvius confirms that [professional accounts require interactive authentication](https://www.fluvius.be/nl/veelgestelde-vragen/mijn-fluvius/organisatie). Its [professional data API](https://partner.fluvius.be/nl/energiedienstverleners/ontsluiten-verbruiksdata-api) is a different service and is not implemented here.

## Configure the Energy dashboard

1. In the integration's options, select **Quarter-hour** for electricity or **Hourly** for gas. Use **Daily** if detailed readings are unavailable. Keep **Days back** at seven or increase it to cover longer publication delays.
2. Wait for the initial import. Open **Settings → Dashboards → Energy** and choose the Fluvius sources whose names end in **(historical)**.
3. Choose **consumption total**, or the high/low tariff pair, for grid consumption. Choose the corresponding injection source for grid return. Do not select both the total and its tariff components.
4. For gas, choose the historical consumption source in the selected unit.

Each consumption/injection sensor exposes its corresponding `historical_statistic_id` attribute. Historical IDs start with `fluvius:` and remain stable when an entry is recreated. Gas kWh and m3 histories have separate IDs; switching units does not relabel existing data.

**Use the historical sources for the Energy dashboard.** Normal entity history shows when Home Assistant received an update and cannot backdate readings. Cumulative display sensors use `total` so downward corrections are not interpreted as meter resets. Interval sensors display the latest available interval, with the latest local day's readings in their attributes; they are not cumulative energy sources.

Home Assistant's supported external statistics API accepts **hourly** records. Electricity quarters are summed into their actual UTC hour, preserving local dates and daylight-saving changes. Gas hours are imported directly. In Daily mode, the whole published daily total is assigned to the hour containing the period start; a daily total cannot supply an hourly breakdown. A complete set of detailed readings replaces that daily allocation when it becomes available. Partial detailed data does not replace an available daily total.

The original interval readings are retained in the integration's history storage, but **15-minute Recorder history and exact quarter-hour dynamic-price calculations are not supported**. Multiplying delayed consumption by the current price sensor is also incorrect. This integration does not calculate historical costs.

## Upgrading from 1.0.x

Replace the integration files and **restart Home Assistant**. In the options, select the desired granularity again: `4` keeps daily readings, anything else enables detailed ones. The detailed API code no longer has to be right, because the integration probes for it (see **Granularity** below).

Remove the old Fluvius sensor sources from the Energy dashboard and select the new **(historical)** sources. Existing entity history, including old spikes, is not rewritten or deleted. The new sources are rebuilt from available Fluvius data in the configured window. Increase **Days back** (up to 31) if needed. Older cached readings outside that window are not automatically migrated.

The integration deduplicates readings by timestamp, updates corrected values in both directions, and retains its import ledger across restarts and entry recreation. Repeated imports do not add consumption again. Keep a backup of your Home Assistant configuration: the ledger under `.storage/fluvius_history_*` is required to retain historical corrections and cumulative totals. It grows as source history is collected.

## Combine historical Fluvius data with a P1 meter

Set **Import history until** to the point where your P1 history begins. This is an **exclusive cutoff**: only complete Fluvius intervals ending at or before that time are included. Choose an interval boundary; the integration does not split an interval across the cutoff. **Days back** determines the window preceding that cutoff. Choose a local midnight cutoff for Daily mode, or enable detailed readings for a cutoff within a day.

A cutoff creates a separate historical source, identified by an `_until_` suffix. Add that source alongside the P1 source in Energy, and remove any unrestricted Fluvius source for the same meter. This avoids counting overlapping periods twice. Data are imported into a separate Fluvius source, not into your P1 sensor. For older history spanning more than 31 days, external CSV import tools remain an option; changing the cutoff creates a different source rather than extending the same one.

Clear the cutoff to resume current history. This restores the unrestricted source; it does not remove previously imported sources from Recorder or your Energy configuration.

## Options and troubleshooting

- **Timezone:** used for request boundaries; default `Europe/Brussels`.
- **Days back:** complete history window per refresh, default seven; gas always uses at least seven days.
- **Granularity:** Daily, electricity Quarter-hour, or gas Hourly. Fluvius does not document its detailed API codes, so this choice is a preference rather than a fixed setting: the integration tries the selected code first, then the known candidates, and keeps whichever actually returns intervals of the meter's own resolution. If none does but a coarser one answers, that resolution is imported instead and a warning names it. Empty detailed responses can still mean that permission is missing or publication is delayed; daily readings continue to work, and the resolved code and per-code probe results appear in the diagnostics download.
- **Gas unit:** selects the actual kWh or m3 readings provided by Fluvius. No fixed conversion factor is applied. After changing it, select the matching historical source in Energy.
- **Verbose logging:** enables request dates and endpoint information. Credentials and raw response bodies are not logged.

Empty responses preserve the latest available display values. An unavailable peak-power endpoint does not prevent consumption updates. Expired credentials trigger Home Assistant's reauthentication flow. Diagnostics include meter identifiers and consumption data, so review the download before sharing it publicly.

## Development

Use Python 3.14 and an isolated virtual environment:

```sh
python -m pip install -r requirements-test.txt
python -m pytest -q
ruff check custom_components/fluvius tests
ruff format --check custom_components/fluvius tests
```

Tests cover config flows, gas units, delayed history, corrections, repeated imports, cutoffs, DST, and actual SQLite Recorder imports. No credentials are needed for the test suite.
