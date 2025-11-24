# Fluvius Energy Integration

This repository packages a Home Assistant custom integration that logs in to Mijn Fluvius and turns the returned consumption/injection history into long-term energy sensors. Both electricity (consumption/injection) and gas (consumption in kWh) meters are supported so the Fluvius data that feeds the official portal can appear inside the Home Assistant Energy dashboard.

## Features

- Automatic refresh of Fluvius consumption and injection totals (tariff-specific or overall)
- Explicit meter-type selection (electricity or gas) so polling and parsing can match Fluvius' delivery cadence
- Works with electricity and gas meters; gas payloads use the kWh values exposed by Fluvius and ignore duplicate m³ readings
- Sensors declared with `state_class=total_increasing`, so they qualify for the Energy dashboard
- Options flow to tweak timezone, lookback window, and Fluvius granularity without re-adding the entry
- Diagnostics endpoint for privacy-safe troubleshooting
- Basic config-flow tests to keep regressions in check

## Repository Layout

```
custom_components/fluvius_energy/   # Integration code (manifest, config flow, sensors, diagnostics)
docs/integration/fluvius_energy.md  # Extended documentation for end users
tests/components/fluvius_energy/    # Pytest-based config-flow coverage
```

Scripts used for the early CLI-based approach are intentionally left out; the integration is self-contained under `custom_components/fluvius_energy`.

## Requirements

- Home Assistant 2024.10 or newer (tested on 2025.10 dev builds)
- A Fluvius account with access to the meter you plan to expose
- Python package `requests` (already declared in `manifest.json`, Home Assistant installs it automatically)

## Installation

1. Download or clone this repository.
2. Copy the entire `custom_components/fluvius_energy` folder to your Home Assistant configuration directory under `custom_components/` (create it if it does not exist).
3. Restart Home Assistant so it discovers the new integration.

When a new version is released, replace the folder with the updated copy and restart Home Assistant again.

## Adding the Integration in Home Assistant

1. Open **Settings → Devices & Services → Add Integration**.
2. Search for **Fluvius Energy**.
3. Enter the same credentials you use on mijn.fluvius.be:
   - Email address
   - Password
   - EAN (the 18-digit meter identifier)
   - Meter serial number
   - Meter type (electricity or gas)
4. Submit the form. The integration validates the credentials by fetching a small history sample before creating the entry.

### Options Flow

After the entry is created, use the **Options** button in the integration card to configure:

- **Timezone**: IANA timezone used to build history date ranges (defaults to `Europe/Brussels`).
- **Days back**: How many days of history to grab per refresh (1–31). Gas entries automatically enforce a 7-day minimum so fresh data appears even with Fluvius' 72-hour gas delay.
- **Granularity**: Fluvius API granularity flag (`3` = quarter-hourly, `4` = daily). Gas entries are automatically forced to daily (`4`) because Fluvius does not expose quarter-hour data for gas meters.
- **Meter type**: Switch between electricity and gas if you replace the hardware later. Changing this updates the config entry and reloads the integration.

Changing any of these values triggers a config-entry reload.

### Energy Dashboard Setup

1. Go to **Settings → Dashboards → Energy → Configure**.
2. Assign sensors:
   - `sensor.fluvius_consumption_total` → Grid consumption (use this sensor for both electricity and gas sources)
   - `sensor.fluvius_injection_total` → Return to grid / production
3. Optional sensors for tariff-specific reporting: `consumption_high`, `consumption_low`, `injection_high`, `injection_low`.
4. A non-cumulative `sensor.fluvius_net_consumption_day` is available for daily comparisons but is not used directly in the Energy dashboard.

### Diagnostics and Reauthentication
- Gas data is only published by Fluvius after ~72 hours. The integration automatically fetches at least the past 7 days for gas meters so newly released measurements are not missed.

- Download diagnostics via **Settings → Devices & Services → Fluvius Energy → ... → Download diagnostics**. The payload includes the configured EAN, meter serial, and sanitized lifetime totals.
- If Fluvius rejects your credentials later, Home Assistant automatically triggers the reauthentication flow. Supply the new email/password and the integration reloads itself.

## Testing

Install the Home Assistant dev environment and run:

```
pytest tests/components/fluvius_energy/test_config_flow.py
```

The suite validates the user setup path, invalid credential handling, and the reauthentication workflow. Add additional tests as you extend the integration (coordinator, sensors, diagnostics, etc.).

## Troubleshooting

- **Config flow cannot be loaded**: Ensure you copied the entire `custom_components/fluvius_energy` directory and restarted Home Assistant. The integration relies on the new selector helpers available in HA 2024.10+.
- **Setup fails with HTTP errors**: Double-check your Fluvius credentials, EAN, and meter serial. You can also run Home Assistant in debug mode and inspect the logs for `FluviusEnergy` entries.
- **Energy dashboard shows no data**: Verify that the sensors report values in Developer Tools → States and that you selected the correct entities under Energy configuration. Remember that Home Assistant may take up to an hour to incorporate new statistics.

For deeper details (including API call structure and CSV export examples) see `docs/integration/fluvius_energy.md`.
